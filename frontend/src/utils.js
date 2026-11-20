export function fmtBand(b) {
  if (!b) return '-'
  if (b.v === null || b.v === undefined) return '未知'
  if (b.lo === b.hi || b.hi === null || b.hi === undefined) {
    return b.hi === null || b.hi === undefined ? `${b.v}` : `${b.v}`
  }
  return `${b.v} [${b.lo} ~ ${b.hi}]`
}

export function fmtVal(v, digits = 2) {
  if (v === null || v === undefined) return '未知'
  return Number(v).toFixed(digits).replace(/\.?0+$/, '')
}

export const QUALITY_TAG = { GOOD: 'success', ESTIMATED: 'warning', UNKNOWN: 'danger' }
export const COMPARABLE_TAG = { FULL: 'success', PARTIAL: 'warning', NONE: 'danger' }
export const CUT_LABEL = {
  START: '起始', STOP: '停车', TURNBACK: '折返', SECTION_EXIT: '出区段',
  CONSIST_CHANGE: '换编组', MULTIPLIER_CHANGE: '倍率切换', DEVICE_RESET: '设备重启', END: '结束',
}
export const SOURCE_LABEL = { ONBOARD: '车载侧', SUPPLY: '供电侧' }
export const Q_LABEL = {
  traction: '牵引', regen: '回馈', aux: '辅助', import: '受电', export: '返送',
}
export const METRIC_LABEL = {
  NET_KWH: '净能耗(kWh)', KWH_PER_KM: '单位里程能耗(kWh/km)',
  KWH_PER_PASSENGER: '单位载客能耗(kWh/人)', REGEN_RATIO: '回馈比例',
}
export const SCOPE_LABEL = { RUN: '运行', SECTION: '区段', SHIFT: '班次', VEHICLE_MODEL: '车型' }

// ---- 设备健康 ----
export const HEALTH_STATE_LABEL = {
  HEALTHY: '健康期', EXCLUDED_PRE_FAULT: '故障前窗口(排除出基线)', POST_FAULT: '故障已发生',
}
export const ELIGIBLE_LABEL = {
  ELIGIBLE: '具备资格', DOWNGRADED: '资格降级', INELIGIBLE: '资格取消',
}
export const ELIGIBLE_TAG = { ELIGIBLE: 'success', DOWNGRADED: 'warning', INELIGIBLE: 'info' }
export const REASON_LABEL = {
  ENERGY_UNKNOWN: '能耗未知', ENERGY_ESTIMATED: '能耗为估计值',
  CONDITION_MISSING: '运行条件缺失', CALIBRATION_FAILED: '校准失效',
  CALIBRATION_UNKNOWN: '校准状态未知', CALIBRATION_EXPIRED: '校准过期',
  POST_FAULT: '故障已确认', NO_BASELINE: '无基线(样本不足)',
}
export const ALERT_TYPE_LABEL = { DEGRADATION: '慢性劣化', SUDDEN: '突发偏离' }
export const ALERT_TYPE_TAG = { DEGRADATION: 'warning', SUDDEN: 'danger' }
export const MAINT_TYPE_LABEL = { FAULT: '故障', REPAIR: '维修', OVERHAUL: '检修' }
export const MAINT_TYPE_TAG = { FAULT: 'danger', REPAIR: 'warning', OVERHAUL: 'info' }
export const CALIB_TAG = { VALID: 'success', EXPIRED: 'warning', FAILED: 'danger' }
export const TRIGGER_LABEL = {
  SEED: '初始分析', MANUAL: '手动触发', LATE_MAINTENANCE: '维修晚到重分析',
}
