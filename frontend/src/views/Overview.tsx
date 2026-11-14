import { useEffect, useState } from 'react'

import { fetchJson } from '@/api/client'

type VersionRecovery = {
  服务组: string
  版本来源: string
  配置来源: string
  恢复前版本: string
  恢复后版本: string
  版本恢复: string
  恢复后服务名称: string
  自动恢复配置项: number
  需审批配置项: number
  禁止撤回配置项: number
  允许撤回变更项: number
  需审批变更项: number
  禁止撤回变更项: number
  预演编号: string | null
  预演状态: string
  当前实例版本: string | null
  待审批回退记录: number
  版本核对?: { 当前值: string; 恢复后取值: string; 版本来源: string; 分类: string; 处理方式: string }
  配置核对?: { 配置项: string; 当前值: string; 目标值: string; 分类: string; 恢复后取值: string; 处理方式: string }[]
  核对结论?: { 版本一致: boolean; 配置一致: boolean; 实例版本一致: boolean; 禁止撤回项: string[]; 结论: string }
}

type Overview = {
  cards: { label: string; value: number }[]
  modules: { name: string; created: number; pending: number; abnormal: number }[]
  versionRecovery?: VersionRecovery
}

const fallbackCards = [{"label": "业务模块", "value": 0}, {"label": "今日新增", "value": 0}]
const fallbackModules = [{"name": "服务注册发现", "created": 0, "pending": 0, "abnormal": 0}, {"name": "查询路由分发", "created": 0, "pending": 0, "abnormal": 0}, {"name": "结果缓存加速", "created": 0, "pending": 0, "abnormal": 0}, {"name": "调用限流降级", "created": 0, "pending": 0, "abnormal": 0}, {"name": "服务编排组合", "created": 0, "pending": 0, "abnormal": 0}, {"name": "调用链追踪", "created": 0, "pending": 0, "abnormal": 0}, {"name": "服务健康诊断", "created": 0, "pending": 0, "abnormal": 0}, {"name": "数据接口契约", "created": 0, "pending": 0, "abnormal": 0}, {"name": "数据订阅推送", "created": 0, "pending": 0, "abnormal": 0}, {"name": "数据加工转换", "created": 0, "pending": 0, "abnormal": 0}, {"name": "数据模拟服务", "created": 0, "pending": 0, "abnormal": 0}, {"name": "接入网关管理", "created": 0, "pending": 0, "abnormal": 0}, {"name": "服务指标采集", "created": 0, "pending": 0, "abnormal": 0}, {"name": "调用来源管理", "created": 0, "pending": 0, "abnormal": 0}, {"name": "负载均衡策略", "created": 0, "pending": 0, "abnormal": 0}, {"name": "服务版本回退", "created": 0, "pending": 0, "abnormal": 0}]

export default function Overview() {
  const [cards, setCards] = useState<Overview['cards']>(fallbackCards)
  const [moduleRows, setModuleRows] = useState<Overview['modules']>(fallbackModules)
  const [recovery, setRecovery] = useState<VersionRecovery | undefined>(undefined)

  useEffect(() => {
    fetchJson<Overview>('/api/overview')
      .then((payload) => {
        setCards(payload.cards)
        setModuleRows(payload.modules)
        setRecovery(payload.versionRecovery)
      })
      .catch(() => undefined)
  }, [])

  return (
    <section className="page">
      <header className="page-head">
        <div>
          <h2>运营概览</h2>
          <p className="page-desc">汇总各业务模块的关键指标，先看总量再看异常。</p>
        </div>
      </header>
      <div className="stat-row">
        {cards.map((card) => (
          <article key={card.label} className="stat-card">
            <span className="stat-label">{card.label}</span>
            <strong className="stat-value">{card.value}</strong>
          </article>
        ))}
      </div>
      <table className="data-table">
        <thead>
          <tr><th>业务模块</th><th>今日新增</th><th>待处理</th><th>异常量</th></tr>
        </thead>
        <tbody>
          {moduleRows.map((row) => (
            <tr key={row.name}>
              <td>{row.name}</td>
              <td>{row.created}</td>
              <td>{row.pending}</td>
              <td>{row.abnormal}</td>
            </tr>
          ))}
        </tbody>
      </table>

      {recovery && (
        <section className="preview-summary" data-module="overview-version-recovery">
          <h4>版本恢复核对（{recovery.服务组 || '服务组'}）</h4>
          <div className="summary-grid">
            <span>版本恢复：<strong>{recovery.版本恢复}</strong></span>
            <span>运行实例版本：<strong>{recovery.当前实例版本 ?? '未注册'}</strong></span>
            <span>预演状态：{recovery.预演状态}{recovery.预演编号 ? `（${recovery.预演编号}）` : ''}</span>
            <span>待审批回退记录：{recovery.待审批回退记录}</span>
          </div>
          <table className="data-table compact">
            <thead>
              <tr><th>核对项</th><th>对照快照</th><th>当前值</th><th>目标值</th><th>分类</th><th>恢复后取值</th><th>处理方式</th></tr>
            </thead>
            <tbody>
              <tr>
                <td>版本号</td>
                <td>{recovery.版本核对?.版本来源 ?? recovery.版本来源}</td>
                <td>{recovery.版本核对?.当前值 ?? recovery.恢复前版本}</td>
                <td>{recovery.恢复后版本}</td>
                <td>{recovery.版本核对?.分类 ?? (recovery.需审批变更项 > 0 ? '需审批' : '允许撤回')}</td>
                <td>{recovery.版本核对?.恢复后取值 ?? recovery.恢复后版本}</td>
                <td>{recovery.版本核对?.处理方式 ?? '审批通过后切换'}</td>
              </tr>
              {(recovery.配置核对 ?? []).map((row) => (
                <tr key={row.配置项}>
                  <td>{row.配置项}</td>
                  <td>{recovery.配置来源}</td>
                  <td>{row.当前值 || '—'}</td>
                  <td>{row.目标值 || '—'}</td>
                  <td>{row.分类}</td>
                  <td>{row.恢复后取值 || '—'}</td>
                  <td>{row.处理方式}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="check-note">
            版本号对照 {recovery.版本来源}，配置项对照 {recovery.配置来源}；本表只展示回退后的取值，审批通过前运行实例版本保持不变。
            {recovery.核对结论 && <strong> {recovery.核对结论.结论}。</strong>}
          </p>
        </section>
      )}
    </section>
  )
}
