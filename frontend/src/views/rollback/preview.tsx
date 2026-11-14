import { useCallback, useEffect, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/rollback/previews'
const CATEGORIES = ['允许撤回', '需审批', '禁止撤回'] as const
type Category = (typeof CATEGORIES)[number]

const CATEGORY_HINT: Record<Category, string> = {
  允许撤回: '随回退直接恢复，不需要额外审批',
  需审批: '提交审批，通过后才随回退执行',
  禁止撤回: '回退时保持当前值，不允许改写',
}

type DiffRow = {
  变更项: string
  类型: string
  当前值: string
  目标值: string
  分类: Category | string
  说明: string
}

type PreviewRecord = {
  id: number
  记录编号: string
  预演编号: string
  快照编号: string
  目标快照: string
  服务组: string
  服务名称: string
  变更项: string
  类型: string
  当前值: string
  目标值: string
  分类: string
  审批状态: string
  处理结果: string
}

type RecoveryCheck = {
  核对项: string
  版本来源?: string
  当前值: string
  目标值: string
  分类: string
  恢复后取值: string
  处理方式: string
  说明: string
}

type ConfigCheck = {
  配置项: string
  当前值: string
  目标值: string
  分类: string
  恢复后取值: string
  处理方式: string
  说明: string
}

type VersionRecovery = {
  服务组: string
  版本来源: string
  配置来源: string
  恢复前版本: string
  恢复后版本: string
  版本恢复: string
  恢复前服务名称: string
  恢复后服务名称: string
  自动恢复配置项: number
  需审批配置项: number
  禁止撤回配置项: number
  允许撤回变更项: number
  需审批变更项: number
  禁止撤回变更项: number
  版本核对?: RecoveryCheck
  配置核对?: ConfigCheck[]
}

type Preview = {
  id?: number
  预演编号: string | null
  预演状态: string
  已保存?: boolean
  创建时间?: string
  更新时间?: string
  当前快照: string
  目标快照: string
  服务组: string
  当前版本: string
  目标版本: string
  差异: Record<string, DiffRow[]>
  版本恢复概览: VersionRecovery
  当前实例版本: string | null
  回退记录: PreviewRecord[]
  待审批回退记录: PreviewRecord[]
}

type SnapshotDetail = {
  快照编号: string
  服务名称: string
  服务组: string
  版本号: string
  配置项: Record<string, string>
  相关预演: string[]
}

const EMPTY_PREVIEW_SLOTS: Record<Category, DiffRow[]> = {
  允许撤回: [],
  需审批: [],
  禁止撤回: [],
}

export default function RollbackPreview() {
  const [preview, setPreview] = useState<Preview | null>(null)
  const [pending, setPending] = useState<PreviewRecord[]>([])
  const [sourceRef, setSourceRef] = useState('ROLL-0002')
  const [targetRef, setTargetRef] = useState('ROLL-0003')
  const [snapshotQuery, setSnapshotQuery] = useState('')
  const [snapshotDetail, setSnapshotDetail] = useState<SnapshotDetail | null>(null)
  const [message, setMessage] = useState('')
  const [errorMessage, setErrorMessage] = useState('')
  const [busy, setBusy] = useState(false)

  const loadPending = useCallback(async () => {
    const response = await request(`${ENDPOINT}/pending`)
    if (!response.ok) {
      throw new Error('待审批回退记录读取失败')
    }
    const payload = await response.json()
    setPending(payload.items ?? [])
  }, [])

  const openPreview = useCallback(async (previewId: string) => {
    const response = await request(`${ENDPOINT}/${previewId}`)
    if (!response.ok) {
      throw new Error('回退预演结果读取失败')
    }
    const payload = (await response.json()) as Preview
    setPreview(payload)
    setSourceRef(payload.当前快照)
    setTargetRef(payload.目标快照)
    await loadPending()
  }, [loadPending])

  // 首次进入时优先打开已有预演结果；没有已保存的预演时只做一次只读比对，
  // 不落库、不生成审批记录，也不改写运行实例；点「开始预演」才会保存预演。
  useEffect(() => {
    let cancelled = false
    async function bootstrap() {
      try {
        const response = await request(ENDPOINT)
        if (!response.ok) {
          throw new Error('回退预演列表读取失败')
        }
        const payload = await response.json()
        const first: Preview | undefined = payload.items?.[0]
        if (cancelled) {
          return
        }
        if (first?.预演编号) {
          await openPreview(first.预演编号)
          return
        }
        const diffResponse = await request(
          `${ENDPOINT}/diff?source=${encodeURIComponent('ROLL-0002')}&target=${encodeURIComponent('ROLL-0003')}`,
        )
        if (diffResponse.ok) {
          const diff = (await diffResponse.json()) as Preview
          if (!cancelled) {
            setPreview(diff)
          }
        }
        if (!cancelled) {
          await loadPending()
        }
      } catch (error) {
        if (!cancelled) {
          setErrorMessage(error instanceof Error ? error.message : '回退预演初始化失败')
        }
      }
    }
    void bootstrap()
    return () => { cancelled = true }
  }, [loadPending, openPreview])

  async function createPreview() {
    setBusy(true)
    setErrorMessage('')
    setMessage('')
    try {
      const response = await request(ENDPOINT, {
        method: 'POST',
        body: JSON.stringify({ values: { 当前快照: sourceRef, 目标快照: targetRef } }),
      })
      const payload = await response.json()
      if (!response.ok || !payload.ok) {
        throw new Error(payload.message || '回退预演创建失败')
      }
      setPreview(payload.entry as Preview)
      setMessage(payload.message)
      await loadPending()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '回退预演创建失败')
    } finally {
      setBusy(false)
    }
  }

  async function runAction(action: string) {
    if (!preview || !preview.预演编号) {
      setErrorMessage('请先点「开始预演」保存预演记录，再提交审批')
      return
    }
    setBusy(true)
    setErrorMessage('')
    setMessage('')
    try {
      const response = await request(`${ENDPOINT}/${preview.预演编号}/actions`, {
        method: 'POST',
        body: JSON.stringify({ values: { action } }),
      })
      const payload = await response.json()
      if (!response.ok || !payload.ok) {
        throw new Error(payload.message || '回退预演动作未生效')
      }
      setPreview(payload.entry as Preview)
      setMessage(payload.message)
      await loadPending()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '回退预演操作失败')
    } finally {
      setBusy(false)
    }
  }

  async function lookupSnapshot() {
    setErrorMessage('')
    setSnapshotDetail(null)
    if (!snapshotQuery.trim()) {
      setErrorMessage('请输入快照编号，例如 ROLL-0002')
      return
    }
    try {
      const response = await request(`/api/rollback/snapshots/${encodeURIComponent(snapshotQuery.trim())}`)
      if (!response.ok) {
        throw new Error(`快照 ${snapshotQuery.trim()} 不存在或已归档`)
      }
      setSnapshotDetail((await response.json()) as SnapshotDetail)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '历史快照查询失败')
    }
  }

  const diffs = preview?.差异 ?? EMPTY_PREVIEW_SLOTS
  const recovery = preview?.版本恢复概览
  const pendingRows = pending
  // 只读比对没有预演编号，此时不能走审批动作。
  const canSubmit = preview?.预演状态 === '预演中' && Boolean(preview.预演编号)
  const canApprove = preview?.预演状态 === '待审批' && Boolean(preview.预演编号)
  const canCancel = preview && preview.预演编号 && !['已通过', '已驳回', '已取消'].includes(preview.预演状态)

  return (
    <section className="preview-panel" data-module="rollback-preview">
      <header className="preview-head">
        <div>
          <h3>回退预演</h3>
          <p className="page-desc">对照两侧快照，把差异分为允许撤回、需审批、禁止撤回三类；审批通过前不改写运行实例版本。</p>
        </div>
        <div className="preview-actions">
          {canSubmit && <button className="btn" type="button" disabled={busy} onClick={() => void runAction('提交审批')}>提交审批</button>}
          {canApprove && <button className="btn primary" type="button" disabled={busy} onClick={() => void runAction('审批通过')}>审批通过</button>}
          {canApprove && <button className="btn" type="button" disabled={busy} onClick={() => void runAction('审批驳回')}>审批驳回</button>}
          {canCancel && <button className="btn ghost" type="button" disabled={busy} onClick={() => void runAction('取消预演')}>取消预演</button>}
        </div>
      </header>

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void createPreview() }}>
        <label className="filter-item">
          <span>当前快照</span>
          <input value={sourceRef} onChange={(event) => setSourceRef(event.target.value)} placeholder="例如 ROLL-0002" />
        </label>
        <label className="filter-item">
          <span>目标快照</span>
          <input value={targetRef} onChange={(event) => setTargetRef(event.target.value)} placeholder="例如 ROLL-0003" />
        </label>
        <button className="btn primary" type="submit" disabled={busy}>开始预演</button>
        <span className="filter-hint">同组版本再次预演会直接打开已有结果</span>
      </form>

      {preview && (
        <>
          <div className="preview-meta">
            <span className={`status-badge status-${preview.预演状态}`}>{preview.预演状态}</span>
            {preview.预演编号
              ? <span>预演编号：{preview.预演编号}</span>
              : <span>只读比对（点「开始预演」保存预演记录）</span>}
            <span>当前快照：{preview.当前快照}（{preview.当前版本}）</span>
            <span>目标快照：{preview.目标快照}（{preview.目标版本}）</span>
            <span>运行实例版本：{preview.当前实例版本 ?? '未注册'}</span>
          </div>

          <div className="preview-grid">
            {CATEGORIES.map((category) => (
              <article key={category} className={`preview-col col-${category}`}>
                <header className="preview-col-head">
                  <strong>{category}</strong>
                  <span className="col-count">{(diffs[category] ?? []).length} 项</span>
                </header>
                <p className="preview-col-hint">{CATEGORY_HINT[category]}</p>
                <ul className="diff-list">
                  {(diffs[category] ?? []).map((row) => (
                    <li key={`${row.类型}-${row.变更项}`} className="diff-item">
                      <div className="diff-title">
                        <span>{row.变更项}</span>
                        <span className="diff-type">{row.类型}</span>
                      </div>
                      <div className="diff-values">
                        <span className="diff-current">{row.当前值 || '—'}</span>
                        <span className="diff-arrow">→</span>
                        <span className="diff-target">{row.目标值 || '—'}</span>
                      </div>
                      <div className="diff-reason">{row.说明}</div>
                    </li>
                  ))}
                  {(diffs[category] ?? []).length === 0 && (
                    <li className="diff-empty">无此类差异</li>
                  )}
                </ul>
              </article>
            ))}
          </div>

          <div className="preview-summary">
            <h4>版本恢复概览</h4>
            <div className="summary-grid">
              <span>版本恢复：<strong>{preview.版本恢复概览.版本恢复}</strong></span>
              <span>服务名称：{preview.版本恢复概览.恢复后服务名称}（禁止撤回，保持当前值）</span>
              <span>自动恢复配置项：{preview.版本恢复概览.自动恢复配置项}</span>
              <span>需审批配置项：{preview.版本恢复概览.需审批配置项}</span>
              <span>禁止撤回配置项：{preview.版本恢复概览.禁止撤回配置项}</span>
              <span>版本核对：{preview.版本恢复概览.版本核对?.恢复后取值 ?? '—'}（对照 {preview.版本恢复概览.版本核对?.版本来源 ?? '—'}）</span>
            </div>
            {recovery && (
              <details className="check-details">
                <summary>按 {recovery.配置来源} 的配置项逐项核对</summary>
                <table className="data-table compact">
                  <thead>
                    <tr><th>配置项</th><th>当前值</th><th>目标值</th><th>分类</th><th>恢复后取值</th><th>处理方式</th></tr>
                  </thead>
                  <tbody>
                    {(recovery.配置核对 ?? []).map((row) => (
                      <tr key={row.配置项}>
                        <td>{row.配置项}</td>
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
                  版本号对照 {recovery.版本来源}：{recovery.版本核对?.当前值} → {recovery.版本核对?.恢复后取值}；
                  配置项对照 {recovery.配置来源}。
                </p>
              </details>
            )}
          </div>
        </>
      )}

      <div className="preview-summary">
        <h4>待审批回退记录清单</h4>
        <table className="data-table compact">
          <thead>
            <tr>
              <th>记录编号</th>
              <th>预演编号</th>
              <th>服务组</th>
              <th>变更项</th>
              <th>当前值</th>
              <th>目标值</th>
              <th>分类</th>
              <th>审批状态</th>
            </tr>
          </thead>
          <tbody>
            {pendingRows.map((row) => (
              <tr key={row.记录编号}>
                <td>{row.记录编号}</td>
                <td>{row.预演编号}</td>
                <td>{row.服务组}</td>
                <td>{row.变更项}</td>
                <td>{row.当前值 || '—'}</td>
                <td>{row.目标值 || '—'}</td>
                <td>{row.分类}</td>
                <td>{row.审批状态}</td>
              </tr>
            ))}
            {pendingRows.length === 0 && (
              <tr><td className="empty-state" colSpan={8}>暂无待审批回退记录，可先创建预演并提交审批</td></tr>
            )}
          </tbody>
        </table>
      </div>

      <div className="preview-summary">
        <h4>历史快照查询</h4>
        <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void lookupSnapshot() }}>
          <label className="filter-item">
            <span>快照编号</span>
            <input value={snapshotQuery} onChange={(event) => setSnapshotQuery(event.target.value)} placeholder="例如 ROLL-0002" />
          </label>
          <button className="btn" type="submit">按原编号查询</button>
        </form>
        {snapshotDetail && (
          <div className="snapshot-card">
            <div><strong>{snapshotDetail.快照编号}</strong> · {snapshotDetail.服务名称} · {snapshotDetail.版本号}</div>
            <div>服务组：{snapshotDetail.服务组}</div>
            <div>配置项：{Object.entries(snapshotDetail.配置项 ?? {}).map(([key, value]) => `${key}=${value}`).join('；') || '—'}</div>
            <div>引用预演：{snapshotDetail.相关预演.length > 0 ? snapshotDetail.相关预演.join('、') : '暂无'}</div>
          </div>
        )}
      </div>

      <footer className="page-foot">
        {message && <span>{message}</span>}
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
