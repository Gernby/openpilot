import zmq
import math
import numpy as np
import time
from selfdrive.controls.lib.pid import PIController
from selfdrive.controls.lib.drive_helpers import MPC_COST_LAT
from selfdrive.controls.lib.lateral_mpc import libmpc_py
from common.numpy_fast import interp
from common.realtime import sec_since_boot
from selfdrive.swaglog import cloudlog
from cereal import car

_DT = 0.01    # 100Hz
_DT_MPC = 0.05  # 20Hz


def calc_states_after_delay(states, v_ego, steer_angle, curvature_factor, steer_ratio, delay):
  states[0].x = v_ego * delay
  states[0].psi = v_ego * curvature_factor * math.radians(steer_angle) / steer_ratio * delay
  return states


def get_steer_max(CP, v_ego):
  return interp(v_ego, CP.steerMaxBP, CP.steerMaxV)


class LatControl(object):
  def __init__(self, VM):
    self.pid = PIController((VM.CP.steerKpBP, VM.CP.steerKpV),
                            (VM.CP.steerKiBP, VM.CP.steerKiV),
                            k_f=VM.CP.steerKf, pos_limit=1.0)
    self.last_cloudlog_t = 0.0
    self.setup_mpc(VM.CP.steerRateCost)

  def setup_mpc(self, steer_rate_cost):
    self.libmpc = libmpc_py.libmpc
    self.libmpc.init(MPC_COST_LAT.PATH, MPC_COST_LAT.LANE, MPC_COST_LAT.HEADING, steer_rate_cost)

    self.mpc_solution = libmpc_py.ffi.new("log_t *")
    self.cur_state = libmpc_py.ffi.new("state_t *")
    self.mpc_updated = False
    self.mpc_nans = False
    self.cur_state[0].x = 0.0
    self.cur_state[0].y = 0.0
    self.cur_state[0].psi = 0.0
    self.cur_state[0].delta = 0.0

    self.last_mpc_ts = 0.0
    self.angle_steers_des = 0.0
    self.angle_steers_des_mpc = 0.0
    self.angle_steers_des_prev = 0.0
    self.angle_steers_des_time = 0.0
    self.context = zmq.Context()
    self.steerpub = self.context.socket(zmq.PUB)
    self.steerpub.bind("tcp://*:8594")
    self.steerdata = ""
    self.ratioExp = 2.9
    self.ratioScale = 20.
    self.steer_steps = [0., 0., 0., 0., 0.]
    self.probFactor = 0.

  def reset(self):
    self.pid.reset()

  def update(self, active, v_ego, angle_steers, steer_override, d_poly, angle_offset, VM, PL):
    cur_time = sec_since_boot()
    self.mpc_updated = False
    # TODO: this creates issues in replay when rewinding time: mpc won't run

    ratioFactor = max(0.1, 1. - self.ratioScale * abs(angle_steers / 100.) ** self.ratioExp)
    cur_Steer_Ratio = VM.CP.steerRatio * ratioFactor

    if self.last_mpc_ts < PL.last_md_ts:
      self.last_mpc_ts = PL.last_md_ts
      self.angle_steers_des_prev = self.angle_steers_des_mpc

      curvature_factor = VM.curvature_factor(v_ego)

      l_poly = libmpc_py.ffi.new("double[4]", list(PL.PP.l_poly))
      r_poly = libmpc_py.ffi.new("double[4]", list(PL.PP.r_poly))
      p_poly = libmpc_py.ffi.new("double[4]", list(PL.PP.p_poly))
      c_poly = libmpc_py.ffi.new("double[4]", list(PL.PP.c_poly))
      
      # account for actuation delay
      self.cur_state = calc_states_after_delay(self.cur_state, v_ego, angle_steers, curvature_factor, cur_Steer_Ratio, VM.CP.steerActuatorDelay)

      v_ego_mpc = max(v_ego, 5.0)  # avoid mpc roughness due to low speed
      self.libmpc.run_mpc(self.cur_state, self.mpc_solution,
                          l_poly, r_poly, p_poly,
                          PL.PP.l_prob, PL.PP.r_prob, PL.PP.p_prob, curvature_factor, v_ego_mpc, PL.PP.lane_width)
      self.ProbFactor = ((PL.PP.r_prob + PL.PP.l_prob) / 2.
      
      # reset to current steer angle if not active or overriding
      if active:
        isActive = 1
        delta_desired = self.mpc_solution[0].delta[1]
      else:
        isActive = 0
        delta_desired = math.radians(angle_steers - angle_offset) / cur_Steer_Ratio

      self.cur_state[0].delta = delta_desired
      self.angle_steers_des_mpc = float(math.degrees(delta_desired * cur_Steer_Ratio) + angle_offset)
        
      self.angle_steers_des_time = cur_time
      self.mpc_updated = True

      #  Check for infeasable MPC solution
      self.mpc_nans = np.any(np.isnan(list(self.mpc_solution[0].delta)))
      t = sec_since_boot()
      if self.mpc_nans:
        self.libmpc.init(MPC_COST_LAT.PATH, MPC_COST_LAT.LANE, MPC_COST_LAT.HEADING, VM.CP.steerRateCost)
        self.cur_state[0].delta = math.radians(angle_steers) / cur_Steer_Ratio

        if t > self.last_cloudlog_t + 5.0:
          self.last_cloudlog_t = t
          cloudlog.warning("Lateral mpc - nan: True")
      
      self.steerdata = ("%d,%f,%f,%f,%f,%f,%f,%f,%f,%f,%f,%f,%f,%f,%f,%f,%f,%f,%f,%f,%f,%f,%f,%f,%f,%f,%f,%f,%f,%f,%f,%f,%f,%f,%f,%f,%d" % (isActive, delta_desired, angle_offset, \
                self.angle_steers_des_mpc, cur_Steer_Ratio, VM.CP.steerKf / ratioFactor, VM.CP.steerKpV[0] / ratioFactor, VM.CP.steerKiV[0] / ratioFactor, VM.CP.steerRateCost, PL.PP.l_prob, \
                PL.PP.r_prob, PL.PP.c_prob, PL.PP.p_prob, l_poly[0], l_poly[1], l_poly[2], l_poly[3], r_poly[0], r_poly[1], r_poly[2], r_poly[3], \
                p_poly[0], p_poly[1], p_poly[2], p_poly[3], PL.PP.c_poly[0], PL.PP.c_poly[1], PL.PP.c_poly[2], PL.PP.c_poly[3], PL.PP.d_poly[0], PL.PP.d_poly[1], \
                PL.PP.d_poly[2], PL.PP.lane_width, PL.PP.lane_width_estimate, PL.PP.lane_width_certainty, v_ego, int(time.time() * 1000000000)))

    elif self.steerdata != "":
      self.steerpub.send(self.steerdata)
      self.steerdata = ""
          
    if v_ego < 0.3 or not active:
      output_steer = 0.0
      self.pid.reset()
      self.steer_steps[int(cur_time * 100) % 5] = 0.
    else:
      # TODO: ideally we should interp, but for tuning reasons we keep the mpc solution
      # constant for 0.05s.
      self.steer_steps[int(cur_time * 100) % 5] = self.angle_steers_des_mpc
      self.angle_steers_des = (self.steer_steps[0] + self.steer_steps[1] + self.steer_steps[2] + self.steer_steps[3] + self.steer_steps[4]) / 5.

      steers_max = get_steer_max(VM.CP, v_ego)
      self.pid.pos_limit = steers_max
      self.pid.neg_limit = -steers_max
      steer_feedforward = self.angle_steers_des   # feedforward desired angle
      if VM.CP.steerControlType == car.CarParams.SteerControlType.torque:
        steer_feedforward *= v_ego**2  # proportional to realigning tire momentum (~ lateral accel)
      deadzone = 0.0

      output_steer = self.pid.update(self.angle_steers_des, angle_steers, ratioFactor=ratioFactor, probFactor=self.ProbFactor, check_saturation=(v_ego > 10), override=steer_override,
                                     feedforward=steer_feedforward, speed=v_ego, deadzone=deadzone)
                                     
    self.sat_flag = self.pid.saturated
    return output_steer, float(self.angle_steers_des)
