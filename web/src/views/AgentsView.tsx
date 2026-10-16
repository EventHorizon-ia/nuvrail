import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Bot, ClipboardList, RefreshCw, Trash2 } from 'lucide-react'
import { fetchAgents, revokeAgent, startGmailOAuth, createAgent, fetchAgentAuditLog } from '../api/client'
import type { AgentResponse, AgentCreateResponse } from '../api/client'
import type { AuditEntry } from '../types'
import ProviderPicker, { type Provider } from '../components/ProviderPicker'

function formatDate(ts: number): string {
  return new Date(ts * 1000).toLocaleDateString(undefined, {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
  })
}

// ---------------------------------------------------------------------------
// Per-agent audit panel
// ---------------------------------------------------------------------------

const AUDIT_EVENT_COLORS: Record<string, string> = {
  staged:           'text-blue-400',
  approved:         'text-emerald-400',
  executed:         'text-emerald-300',
  rejected:         'text-red-400',
  execution_failed: 'text-orange-400',
  expired:          'text-fg-3',
  auto_rule:        'text-accent',
}

function AgentAuditPanel({ agentId }: { agentId: number }) {
  const { data, isLoading, isError } = useQuery({
    queryKey: ['agent-audit', agentId],
    queryFn: () => fetchAgentAuditLog(agentId, { limit: 10 }),
  })

  if (isLoading) {
    return <p className="text-fg-3 text-xs py-2">Loading audit…</p>
  }
  if (isError) {
    return <p className="text-red-400 text-xs py-2">Failed to load audit log.</p>
  }
  const entries: AuditEntry[] = data?.entries ?? []
  if (entries.length === 0) {
    return <p className="text-fg-3 text-xs py-2 italic">No audit entries yet.</p>
  }

  return (
    <div className="mt-2 space-y-1">
      {entries.map((entry) => (
        <div key={entry.id} className="flex items-center gap-2 text-xs">
          <span className="text-fg-3 shrink-0">
            {new Date(entry.timestamp * 1000).toLocaleString(undefined, {
              month: 'short', day: 'numeric',
              hour: '2-digit', minute: '2-digit',
            })}
          </span>
          <span className={`font-medium shrink-0 ${AUDIT_EVENT_COLORS[entry.event] ?? 'text-fg-2'}`}>
            {entry.event.replace('_', ' ')}
          </span>
          {entry.op_type && (
            <span className="text-fg-3 shrink-0">{entry.op_type}</span>
          )}
          {entry.op_description && (
            <span className="text-fg-3 truncate">{entry.op_description}</span>
          )}
        </div>
      ))}
      {(data?.total ?? 0) > 10 && (
        <p className="text-fg-3 text-xs pt-1">
          Showing 10 of {data!.total} entries — use the Audit view for the full log.
        </p>
      )}
    </div>
  )
}

function AgentCard({
  agent,
  onDelete,
  deleting,
}: {
  agent: AgentResponse
  onDelete: (id: number) => void
  deleting: boolean
}) {
  const [confirming, setConfirming] = useState(false)
  const [showAudit, setShowAudit] = useState(false)
  const isRevoked = agent.revoked_at != null

  return (
    <div
      className={`bg-surface border rounded-lg p-4 flex flex-col gap-2 ${
        isRevoked ? 'border-edge opacity-60' : 'border-edge'
      }`}
    >
      {/* Header row */}
      <div className="flex items-start justify-between gap-2">
        <div className="flex items-center gap-2 min-w-0">
          <Bot className="w-4 h-4 text-accent shrink-0" />
          <span className="font-medium text-fg truncate">
            {agent.label || 'Unnamed agent'}
          </span>
          {isRevoked && (
            <span className="text-xs bg-red-900 text-red-300 px-1.5 py-0.5 rounded shrink-0">
              Revoked
            </span>
          )}
        </div>

        {!isRevoked && (
          confirming ? (
            <div className="flex gap-2 shrink-0">
              <button
                onClick={() => { onDelete(agent.id); setConfirming(false) }}
                disabled={deleting}
                className="text-xs bg-red-700 hover:bg-red-600 disabled:opacity-50 text-white px-2 py-1 rounded"
              >
                {deleting ? 'Revoking…' : 'Confirm'}
              </button>
              <button
                onClick={() => setConfirming(false)}
                className="text-xs bg-slate-600 hover:bg-slate-500 text-fg-2 px-2 py-1 rounded"
              >
                Cancel
              </button>
            </div>
          ) : (
            <button
              onClick={() => setConfirming(true)}
              className="text-fg-3 hover:text-red-400 shrink-0 transition-colors"
              title="Revoke agent"
            >
              <Trash2 className="w-4 h-4" />
            </button>
          )
        )}
      </div>

      {/* Details */}
      <div className="font-mono text-xs text-fg-3 space-y-1">
        <div>
          <span className="text-fg-3">Username: </span>
          <span className="text-accent">{agent.agent_username}</span>
        </div>
        <div>
          <span className="text-fg-3">Upstream: </span>
          {agent.upstream_user}@{agent.upstream_host}
        </div>
        <div>
          <span className="text-fg-3">Created: </span>
          {formatDate(agent.created_at)}
        </div>
        {isRevoked && agent.revoked_at && (
          <div>
            <span className="text-fg-3">Revoked: </span>
            {formatDate(agent.revoked_at)}
          </div>
        )}
        <div className="flex items-center gap-1.5">
          <span className={`inline-block w-1.5 h-1.5 rounded-full ${
            agent.last_activity_at ? 'bg-emerald-400' : 'bg-slate-600'
          }`} />
          <span className={agent.last_activity_at ? 'text-emerald-400' : 'text-fg-3'}>
            {agent.last_activity_at
              ? `Last active ${formatDate(agent.last_activity_at)}`
              : 'Never connected'}
          </span>
        </div>
      </div>

      {/* Audit tab */}
      <div className="border-t border-edge pt-2">
        <button
          onClick={() => setShowAudit((v) => !v)}
          className="flex items-center gap-1.5 text-xs text-fg-3 hover:text-fg-2 transition-colors"
        >
          <ClipboardList className="w-3.5 h-3.5" />
          {showAudit ? 'Hide audit' : 'Recent audit'}
        </button>
        {showAudit && <AgentAuditPanel agentId={agent.id} />}
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Add Agent modal — ProviderPicker + per-provider forms
// ---------------------------------------------------------------------------

function AddAgentModal({ onCreated, onClose }: { onCreated: () => void; onClose: () => void }) {
  const [provider, setProvider] = useState<Provider | null>(null)

  const [label, setLabel] = useState('')
  const [upstreamHost, setUpstreamHost] = useState('')
  const [upstreamSmtpHost, setUpstreamSmtpHost] = useState('')
  const [imapPort, setImapPort] = useState(993)
  const [smtpPort, setSmtpPort] = useState(587)
  const [upstreamUser, setUpstreamUser] = useState('')
  const [upstreamPassword, setUpstreamPassword] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [result, setResult] = useState<AgentCreateResponse | null>(null)
  const [copied, setCopied] = useState(false)
  const [gmailLabel, setGmailLabel] = useState('')
  const [gmailLoading, setGmailLoading] = useState(false)
  const [gmailError, setGmailError] = useState<string | null>(null)

  function mapConnectionError(message: string, prov: Provider | null): string {
    if (message.includes('imap_auth_failed')) {
      if (prov === 'icloud') return 'Wrong app-specific password. Generate one at appleid.apple.com → Sign-In and Security → App-Specific Passwords.'
      return 'Wrong username or password.'
    }
    if (message.includes('imap_connection_failed')) return 'Could not reach the IMAP server. Check the host and IMAP port.'
    if (message.includes('imap_tls_verify_failed')) return 'The mail server\'s TLS certificate could not be verified. Check the IMAP host name.'
    if (message.includes('imap_ssl_error')) return 'SSL/TLS error. Check that you are using the correct IMAP SSL port.'
    if (message.includes('imap_timeout')) return 'Connection timed out while verifying. Please try again.'
    return message
  }

  function reset() {
    setProvider(null)
    setLabel('')
    setUpstreamHost('')
    setUpstreamSmtpHost('')
    setImapPort(993)
    setSmtpPort(587)
    setUpstreamUser('')
    setUpstreamPassword('')
    setError(null)
    setResult(null)
    setCopied(false)
    setGmailLabel('')
    setGmailError(null)
  }

  async function handleGmailConnect() {
    setGmailError(null)
    setGmailLoading(true)
    try {
      const res = await startGmailOAuth(gmailLabel.trim() || undefined)
      window.location.href = res.auth_url
    } catch (err) {
      setGmailLoading(false)
      const msg = err instanceof Error ? err.message : String(err)
      setGmailError(msg.includes('oauth2_not_configured')
        ? 'Gmail OAuth2 is not configured on this server.' : msg)
    }
  }

  function handleProviderSelect(p: Provider) {
    setProvider(p)
    // Pre-fill host for known providers
    if (p === 'icloud') { setUpstreamHost('imap.mail.me.com'); setUpstreamSmtpHost('smtp.mail.me.com'); setImapPort(993); setSmtpPort(587) }
    if (p === 'other') { setUpstreamHost(''); setUpstreamSmtpHost(''); setImapPort(993); setSmtpPort(587) }
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    setError(null)
    setLoading(true)
    try {
      const data = await createAgent({
        label: label.trim() || undefined,
        upstream_host: upstreamHost,
        upstream_smtp_host: upstreamSmtpHost.trim() || undefined,
        upstream_imap_port: imapPort,
        upstream_smtp_port: smtpPort,
        upstream_user: upstreamUser,
        upstream_password: upstreamPassword,
      })
      setResult(data)
      // Do NOT call onCreated() here — the user must see and copy their credentials
      // before the modal closes. onCreated() is called from the Done button instead.
    } catch (err) {
      setError(err instanceof Error ? mapConnectionError(err.message, provider) : 'Failed to create agent')
    } finally {
      setLoading(false)
    }
  }

  const proxyHost = import.meta.env.VITE_PROXY_HOST ?? 'nuvrail.example.com'

  function buildConfigSnippet(): string {
    if (!result) return ''

    const host = result.upstream_host?.toLowerCase() ?? ''
    const isGmail = host.includes('gmail')
    const isOutlook = host.includes('outlook') || host.includes('office365') || host.includes('hotmail')
    const isICloud = host.includes('icloud') || host.includes('me.com') || host.includes('mac.com')

    const providerHints: string[] = []
    if (isGmail) {
      providerHints.push(
        ``,
        `# GMAIL-SPECIFIC OPERATIONS`,
        `# Archive a message : UID MOVE <uid> [Gmail]/All Mail`,
        `# Trash a message   : UID MOVE <uid> [Gmail]/Trash`,
        `# Sent folder       : [Gmail]/Sent Mail`,
        `# Drafts folder     : [Gmail]/Drafts`,
        `# Do NOT use COPY + STORE \\Deleted to move messages.`,
        `# Use UID MOVE directly — it stages a single clean operation.`,
      )
    } else if (isOutlook) {
      providerHints.push(
        ``,
        `# OUTLOOK / OFFICE 365 OPERATIONS`,
        `# Archive a message : UID MOVE <uid> Archive`,
        `# Trash a message   : UID MOVE <uid> Deleted Items`,
        `# Sent folder       : Sent Items`,
        `# Junk folder       : Junk Email`,
        `# Use UID MOVE directly; do not use COPY + STORE \\Deleted.`,
      )
    } else if (isICloud) {
      providerHints.push(
        ``,
        `# ICLOUD MAIL OPERATIONS`,
        `# Archive a message : UID MOVE <uid> Archive`,
        `# Trash a message   : UID MOVE <uid> Deleted Messages`,
        `# Sent folder       : Sent Messages`,
        `# Use UID MOVE directly; do not use COPY + STORE \\Deleted.`,
      )
    }

    return [
      `# Nuvrail proxy — connection and operating instructions`,
      `#`,
      `# CONNECTION`,
      `IMAP  ${proxyHost}:993  (SSL/TLS)`,
      `SMTP  ${proxyHost}:465  (SSL/TLS)`,
      `User  ${result.agent_username}`,
      `Pass  ${result.agent_token}`,
      ``,
      `# HOW THIS PROXY WORKS`,
      `# Every write operation (flag changes, moves, copies, email sends) is`,
      `# staged for human approval before it executes on the real mail server.`,
      `# When you receive OK [STAGED], the operation is queued — not yet done.`,
      `# Wait for explicit confirmation before treating it as complete.`,
      `#`,
      `# REQUIRED — always use UID-prefixed commands for all write operations:`,
      `#   UID STORE, UID MOVE, UID COPY`,
      `# Non-UID write commands are rejected with NO [CLIENTBUG].`,
      `#`,
      `# EXPUNGE is permanently blocked by the proxy.`,
      `# To delete a message, use UID MOVE <uid> <Trash folder> instead.`,
      ...providerHints,
    ].join('\n')
  }

  async function copyToken() {
    if (!result) return
    await navigator.clipboard.writeText(result.agent_token)
    setCopied(true)
    setTimeout(() => setCopied(false), 2000)
  }

  async function copySnippet() {
    await navigator.clipboard.writeText(buildConfigSnippet())
    setCopied(true)
    setTimeout(() => setCopied(false), 2000)
  }

  // Outer modal shell — provider picker or per-provider form
  if (!provider) {
    return (
      <div className="bg-surface border border-edge rounded-xl p-5">
        <div className="flex items-center justify-between mb-4">
          <span className="text-xs font-mono text-fg-3 uppercase tracking-wider">Add agent</span>
          <button onClick={onClose} className="text-fg-3 hover:text-fg-2 text-sm">✕</button>
        </div>
        <ProviderPicker
          onSelect={handleProviderSelect}
          heading="Connect an email account"
          subheading="Choose your provider. The agent will connect through the Nuvrail proxy."
        />
      </div>
    )
  }

  // Gmail flow
  if (provider === 'gmail') {
    return (
      <div className="bg-surface border border-edge rounded-xl p-5 space-y-4">
        <div className="flex items-center justify-between">
          <h2 className="text-base font-display font-black text-fg">Connect Gmail</h2>
          <button onClick={reset} className="text-fg-3 hover:text-fg-2 text-sm">✕</button>
        </div>
        <p className="text-sm text-fg-3">You'll be redirected to Google to authorise access.</p>
        <div>
          <label className="block text-sm font-medium text-fg-2 mb-1">Label <span className="text-fg-3 font-normal">(optional)</span></label>
          <input type="text" value={gmailLabel} onChange={(e) => setGmailLabel(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') void handleGmailConnect() }}
            className="w-full bg-surface-hi border border-edge rounded px-3 py-2 text-fg placeholder-fg-3 focus:outline-none focus:ring-2 focus:ring-accent"
            placeholder="e.g. Work, Personal" />
        </div>
        {gmailError && <p className="text-red-400 text-sm">{gmailError}</p>}
        <button onClick={() => void handleGmailConnect()} disabled={gmailLoading}
          className="w-full bg-accent hover:bg-accent-hi disabled:opacity-50 text-white dark:text-[#111c27] font-semibold py-2 px-4 rounded transition-colors">
          {gmailLoading ? 'Redirecting to Google…' : 'Continue with Google'}
        </button>
        <button onClick={reset} className="w-full text-sm text-fg-3 hover:text-fg-2 py-1 transition-colors">← Back</button>
      </div>
    )
  }

  if (result) {
    return (
      <div className="bg-surface border border-edge rounded-lg p-5 space-y-4">
        <h2 className="text-base font-semibold text-fg-2">Agent credentials generated</h2>
        <div className="bg-surface-hi border border-amber-500 rounded-lg p-4 font-mono text-sm space-y-2">
          <p className="text-amber-400 font-semibold mb-3">⚠ Shown once — save these now</p>
          <div className="text-fg-2"><span className="text-fg-3">IMAP server: </span>{proxyHost}</div>
          <div className="text-fg-2"><span className="text-fg-3">IMAP port:   </span>993 <span className="text-fg-3 text-xs">(SSL/TLS)</span></div>
          <div className="text-fg-2"><span className="text-fg-3">SMTP server: </span>{proxyHost}</div>
          <div className="text-fg-2"><span className="text-fg-3">SMTP port:   </span>465 <span className="text-fg-3 text-xs">(SSL/TLS)</span></div>
          <div className="text-fg-2"><span className="text-fg-3">Username:    </span><span className="text-accent">{result.agent_username}</span></div>
          <div className="flex items-center gap-2">
            <span className="text-fg-3">Password:    </span>
            <span className="text-accent break-all">{result.agent_token}</span>
            <button
              onClick={() => void copyToken()}
              className="ml-2 shrink-0 bg-slate-600 hover:bg-slate-500 text-fg-2 text-xs px-2 py-1 rounded"
            >
              {copied ? 'Copied!' : 'Copy'}
            </button>
          </div>
        </div>
        <div className="rounded-md bg-bg/60 border border-edge p-3">
          <div className="flex items-center justify-between mb-2">
            <span className="text-xs font-medium text-fg-3">Config snippet — paste into your IMAP/SMTP client</span>
            <button
              onClick={() => void copySnippet()}
              className="text-xs bg-surface-hi hover:bg-edge text-fg-2 px-2 py-1 rounded"
            >
              {copied ? 'Copied!' : 'Copy all'}
            </button>
          </div>
          <pre className="text-xs text-fg-2 whitespace-pre overflow-x-auto">{buildConfigSnippet()}</pre>
        </div>
        <div className="flex gap-3">
          <button
            onClick={() => { setResult(null); setProvider(null); setLabel(''); setUpstreamHost(''); setUpstreamSmtpHost(''); setUpstreamUser(''); setUpstreamPassword('') }}
            className="bg-surface-hi hover:bg-edge text-fg-2 text-sm font-medium py-2 px-4 rounded"
          >
            Add another
          </button>
          <button onClick={() => { reset(); onCreated() }} className="bg-accent hover:bg-accent-hi text-white dark:text-[#111c27] text-sm font-medium py-2 px-4 rounded">
            Done
          </button>
        </div>
      </div>
    )
  }

  const isICloud = provider === 'icloud'

  return (
    <div className="bg-surface border border-edge rounded-xl p-5">
      <div className="flex items-center justify-between mb-4">
        <h2 className="text-base font-display font-black text-fg">
          {isICloud ? 'Connect iCloud Mail' : 'Connect via IMAP'}
        </h2>
        <button onClick={onClose} className="text-fg-3 hover:text-fg-2 text-sm">✕</button>
      </div>
      {isICloud && (
        <p className="text-sm text-fg-3 mb-4">
          Use an app-specific password from{' '}
          <a href="https://appleid.apple.com" target="_blank" rel="noreferrer" className="text-accent hover:underline">appleid.apple.com</a>
          {' '}→ Sign-In and Security → App-Specific Passwords.
        </p>
      )}
      {!isICloud && (
        <p className="text-sm text-fg-3 mb-4">Enter your IMAP server details. Any standard IMAP/SMTP provider works.</p>
      )}
      <form onSubmit={(e) => void handleSubmit(e)} className="space-y-4">
        <div>
          <label className="block text-sm font-medium text-fg-2 mb-1">
            Label <span className="text-fg-3">(optional — e.g. Work, iCloud)</span>
          </label>
          <input type="text" value={label} onChange={(e) => setLabel(e.target.value)}
            className="w-full bg-surface-hi border border-edge rounded px-3 py-2 text-fg focus:outline-none focus:ring-2 focus:ring-accent"
            placeholder="Work" />
        </div>
        {isICloud ? (
          <div>
            <label className="block text-sm font-medium text-fg-2 mb-1">Mail servers</label>
            <p className="text-xs text-fg-3 font-mono bg-surface-hi border border-edge rounded px-3 py-2">
              IMAP&nbsp; imap.mail.me.com:993 &nbsp;·&nbsp; SMTP&nbsp; smtp.mail.me.com:587
            </p>
          </div>
        ) : (
          <div className="flex gap-4">
            <div className="flex-1">
              <label className="block text-sm font-medium text-fg-2 mb-1">IMAP host</label>
              <input type="text" value={upstreamHost} onChange={(e) => setUpstreamHost(e.target.value)} required
                autoComplete="off"
                className="w-full bg-surface-hi border border-edge rounded px-3 py-2 text-fg placeholder-fg-3 focus:outline-none focus:ring-2 focus:ring-accent"
                placeholder="imap.example.com" />
            </div>
            <div className="flex-1">
              <label className="block text-sm font-medium text-fg-2 mb-1">
                SMTP host <span className="text-fg-3 font-normal">(if different)</span>
              </label>
              <input type="text" value={upstreamSmtpHost} onChange={(e) => setUpstreamSmtpHost(e.target.value)}
                autoComplete="off"
                className="w-full bg-surface-hi border border-edge rounded px-3 py-2 text-fg placeholder-fg-3 focus:outline-none focus:ring-2 focus:ring-accent"
                placeholder="smtp.example.com" />
            </div>
          </div>
        )}
        <div className="flex gap-4">
          <div className="flex-1">
            <label className="block text-sm font-medium text-fg-2 mb-1">IMAP port</label>
            <input type="number" value={imapPort} onChange={(e) => setImapPort(Number(e.target.value))}
              className="w-full bg-surface-hi border border-edge rounded px-3 py-2 text-fg focus:outline-none focus:ring-2 focus:ring-accent" />
          </div>
          <div className="flex-1">
            <label className="block text-sm font-medium text-fg-2 mb-1">SMTP port</label>
            <input type="number" value={smtpPort} onChange={(e) => setSmtpPort(Number(e.target.value))}
              className="w-full bg-surface-hi border border-edge rounded px-3 py-2 text-fg focus:outline-none focus:ring-2 focus:ring-accent" />
          </div>
        </div>
        <div>
          <label className="block text-sm font-medium text-fg-2 mb-1">Email address</label>
          <input
            type="text"
            inputMode="email"
            autoComplete="off"
            value={upstreamUser}
            onChange={(e) => setUpstreamUser(e.target.value)}
            required
            className="w-full bg-surface-hi border border-edge rounded px-3 py-2 text-fg placeholder-fg-3 focus:outline-none focus:ring-2 focus:ring-accent"
            placeholder="you@example.com"
          />
        </div>
        <div>
          <label className="block text-sm font-medium text-fg-2 mb-1">
            {isICloud ? 'App-specific password' : 'Password'}
          </label>
          <input
            type="password"
            autoComplete="new-password"
            value={upstreamPassword}
            onChange={(e) => setUpstreamPassword(e.target.value)}
            required
            className="w-full bg-surface-hi border border-edge rounded px-3 py-2 text-fg focus:outline-none focus:ring-2 focus:ring-accent"
          />
        </div>
        {error && <p className="text-red-400 text-sm">{error}</p>}
        <button type="submit" disabled={loading}
          className="w-full bg-accent hover:bg-accent-hi disabled:opacity-50 text-white font-medium py-2 px-4 rounded transition-colors">
          {loading ? 'Verifying connection…' : 'Connect'}
        </button>
      </form>
    </div>
  )
}

// ---------------------------------------------------------------------------
// AgentsView
// ---------------------------------------------------------------------------

export default function AgentsView() {
  const qc = useQueryClient()
  const [showAddModal, setShowAddModal] = useState(false)

  const { data, isLoading, isError, refetch } = useQuery({
    queryKey: ['agents'],
    queryFn: fetchAgents,
  })

  const deleteMutation = useMutation({
    mutationFn: revokeAgent,
    onSuccess: () => qc.invalidateQueries({ queryKey: ['agents'] }),
  })

  const agents = data ?? []
  const active = agents.filter((a) => a.revoked_at == null)
  const revoked = agents.filter((a) => a.revoked_at != null)

  return (
    <div className="space-y-4">
      {/* Header */}
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold text-fg">Agents</h1>
        <div className="flex items-center gap-2">
          <button
            onClick={() => refetch()}
            className="text-fg-3 hover:text-fg-2 transition-colors"
            title="Refresh"
          >
            <RefreshCw className="w-4 h-4" />
          </button>
          <button
            onClick={() => setShowAddModal(true)}
            className="flex items-center gap-1.5 bg-accent hover:bg-accent-hi text-white dark:text-[#111c27] text-sm font-semibold px-3 py-1.5 rounded transition-colors"
          >
            + Add agent
          </button>
        </div>
      </div>

      {/* Add agent modal */}
      {showAddModal && (
        <AddAgentModal
          onCreated={() => {
            qc.invalidateQueries({ queryKey: ['agents'] })
            setShowAddModal(false)
          }}
          onClose={() => setShowAddModal(false)}
        />
      )}

      {/* Loading */}
      {isLoading && (
        <p className="text-fg-3 text-sm">Loading agents…</p>
      )}

      {/* Error */}
      {isError && (
        <p className="text-red-400 text-sm">Failed to load agents.</p>
      )}

      {/* Empty state */}
      {!isLoading && !isError && agents.length === 0 && (
        <div className="flex flex-col items-center justify-center py-20 text-fg-3 gap-3">
          <Bot className="w-10 h-10 opacity-40" />
          <p className="text-sm">No agents yet — click <strong className="text-fg-2">+ Add agent</strong> to connect your first email account.</p>
        </div>
      )}

      {/* Active agents */}
      {active.length > 0 && (
        <div className="space-y-3">
          {active.map((agent) => (
            <AgentCard
              key={agent.id}
              agent={agent}
              onDelete={(id) => deleteMutation.mutate(id)}
              deleting={deleteMutation.isPending && deleteMutation.variables === agent.id}
            />
          ))}
        </div>
      )}

      {/* Revoked agents (collapsed section) */}
      {revoked.length > 0 && (
        <details className="mt-4">
          <summary className="text-fg-3 text-sm cursor-pointer hover:text-fg-2 select-none">
            {revoked.length} revoked agent{revoked.length !== 1 ? 's' : ''}
          </summary>
          <div className="mt-3 space-y-3">
            {revoked.map((agent) => (
              <AgentCard
                key={agent.id}
                agent={agent}
                onDelete={(id) => deleteMutation.mutate(id)}
                deleting={false}
              />
            ))}
          </div>
        </details>
      )}
    </div>
  )
}
