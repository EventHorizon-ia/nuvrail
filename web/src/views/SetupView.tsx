/**
 * SetupView — two-step first-time onboarding wizard.
 *
 *  Step 1: Create account (email + password)
 *  Step 2: Connect email provider (ProviderPicker → per-provider form)
 *
 * After step 2 the user lands on the main app with their first agent connected.
 * Step 2 can be skipped — they can add agents later from the Agents view.
 */
import { useState, useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import { registerUser, setToken, startGmailOAuth, createAgent, getConfig, type SignupMode } from '../api/client'
import ProviderPicker, { type Provider } from '../components/ProviderPicker'

// ── Step 1: account creation ──────────────────────────────────────────────

// Shown when the deployment is closed to self-signup. UX only — /auth/register
// remains the real gate (it 403s a closed deployment regardless of this UI).
export function ClosedSignupNotice() {
  return (
    <div className="space-y-4">
      <h2 className="text-xl font-display font-black text-fg">Signups are closed</h2>
      <p className="text-sm text-fg-3">
        This Nuvrail deployment is private — new accounts aren't open to
        self-signup. Contact your administrator to have an account provisioned.
      </p>
      <p className="text-center text-sm text-fg-3">
        Already have an account?{' '}
        <a href="#/login" className="text-accent hover:underline">Sign in</a>
      </p>
    </div>
  )
}

function AccountStep({ mode, onComplete }: { mode: SignupMode; onComplete: () => void }) {
  const [email, setEmail] = useState('')
  const [displayName, setDisplayName] = useState('')
  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [inviteCode, setInviteCode] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    setError(null)
    if (password !== confirm) { setError('Passwords do not match.'); return }
    if (password.length < 12) { setError('Password must be at least 12 characters.'); return }
    if (mode === 'invite' && !inviteCode.trim()) { setError('An invite code is required.'); return }
    setLoading(true)
    try {
      const data = await registerUser({
        email,
        display_name: displayName,
        password,
        ...(mode === 'invite' ? { invite_code: inviteCode.trim() } : {}),
      })
      if (data.token) setToken(data.token)
      onComplete()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Registration failed.')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="space-y-5">
      <div>
        <h2 className="text-xl font-display font-black text-fg">Create your account</h2>
        <p className="text-sm text-fg-3 mt-1">
          {mode === 'invite'
            ? 'This deployment is invite-only — enter your invite code below.'
            : 'Set up Nuvrail for the first time.'}
        </p>
      </div>

      <form onSubmit={handleSubmit} className="space-y-4">
        <div>
          <label className="block text-sm font-medium text-fg-2 mb-1">Email</label>
          <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} required
            className="w-full bg-surface-hi border border-edge rounded px-3 py-2 text-fg placeholder-fg-3 focus:outline-none focus:ring-2 focus:ring-accent"
            placeholder="you@example.com" autoComplete="email" />
        </div>
        <div>
          <label className="block text-sm font-medium text-fg-2 mb-1">
            Display name <span className="text-fg-3 font-normal">(optional)</span>
          </label>
          <input type="text" value={displayName} onChange={(e) => setDisplayName(e.target.value)}
            className="w-full bg-surface-hi border border-edge rounded px-3 py-2 text-fg placeholder-fg-3 focus:outline-none focus:ring-2 focus:ring-accent"
            placeholder="Your name" autoComplete="name" />
        </div>
        <div>
          <label className="block text-sm font-medium text-fg-2 mb-1">Password</label>
          <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} required
            className="w-full bg-surface-hi border border-edge rounded px-3 py-2 text-fg placeholder-fg-3 focus:outline-none focus:ring-2 focus:ring-accent"
            autoComplete="new-password" />
          <p className="text-xs text-fg-3 mt-1">Minimum 12 characters.</p>
        </div>
        <div>
          <label className="block text-sm font-medium text-fg-2 mb-1">Confirm password</label>
          <input type="password" value={confirm} onChange={(e) => setConfirm(e.target.value)} required
            className="w-full bg-surface-hi border border-edge rounded px-3 py-2 text-fg placeholder-fg-3 focus:outline-none focus:ring-2 focus:ring-accent"
            autoComplete="new-password" />
        </div>
        {mode === 'invite' && (
          <div>
            <label htmlFor="invite-code" className="block text-sm font-medium text-fg-2 mb-1">Invite code</label>
            <input id="invite-code" type="text" value={inviteCode} onChange={(e) => setInviteCode(e.target.value)} required
              className="w-full bg-surface-hi border border-edge rounded px-3 py-2 text-fg placeholder-fg-3 focus:outline-none focus:ring-2 focus:ring-accent"
              placeholder="Enter your invite code" autoComplete="off" />
          </div>
        )}
        {error && <p className="text-red-400 text-sm">{error}</p>}
        <button type="submit" disabled={loading}
          className="w-full bg-accent hover:bg-accent-hi disabled:opacity-50 text-white dark:text-[#111c27] font-semibold py-2 px-4 rounded transition-colors">
          {loading ? 'Creating account…' : 'Create account'}
        </button>
      </form>

      <p className="text-center text-sm text-fg-3">
        Already have an account?{' '}
        <a href="#/login" className="text-accent hover:underline">Sign in</a>
      </p>
    </div>
  )
}

// ── Step 2: provider-specific connect forms ───────────────────────────────

function GmailForm({ onSkip }: { onSkip: () => void }) {
  const [label, setLabel] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function handleConnect() {
    setError(null)
    setLoading(true)
    try {
      const res = await startGmailOAuth(label.trim() || undefined)
      window.location.href = res.auth_url
    } catch (err) {
      setLoading(false)
      const msg = err instanceof Error ? err.message : String(err)
      setError(msg.includes('oauth2_not_configured')
        ? 'Gmail OAuth2 is not configured on this server.'
        : msg)
    }
  }

  return (
    <div className="space-y-4">
      <div>
        <label className="block text-sm font-medium text-fg-2 mb-1">
          Label <span className="text-fg-3 font-normal">(optional)</span>
        </label>
        <input type="text" value={label} onChange={(e) => setLabel(e.target.value)}
          className="w-full bg-surface-hi border border-edge rounded px-3 py-2 text-fg placeholder-fg-3 focus:outline-none focus:ring-2 focus:ring-accent"
          placeholder="e.g. Work, Personal" />
      </div>
      {error && <p className="text-red-400 text-sm">{error}</p>}
      <button onClick={() => void handleConnect()} disabled={loading}
        className="w-full bg-accent hover:bg-accent-hi disabled:opacity-50 text-white dark:text-[#111c27] font-semibold py-2 px-4 rounded transition-colors">
        {loading ? 'Redirecting to Google…' : 'Continue with Google'}
      </button>
      <button onClick={onSkip} className="w-full text-sm text-fg-3 hover:text-fg-2 transition-colors py-1">
        Skip for now — add later in Agents
      </button>
    </div>
  )
}

function ImapForm({
  prefilledHost,
  prefilledSmtpHost,
  prefilledPort,
  placeholder,
  onSuccess,
  onSkip,
}: {
  prefilledHost: string
  prefilledSmtpHost?: string
  prefilledPort: number
  placeholder: string
  onSuccess: () => void
  onSkip: () => void
}) {
  const [label, setLabel] = useState('')
  const [host, setHost] = useState(prefilledHost)
  const [imapPort, setImapPort] = useState(prefilledPort)
  const [smtpPort, setSmtpPort] = useState(587)
  const [user, setUser] = useState('')
  const [password, setPassword] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  function mapError(msg: string): string {
    if (msg.includes('imap_auth_failed')) return 'Wrong password — use an app-specific password, not your account password.'
    if (msg.includes('imap_connection_failed')) return 'Could not reach the IMAP server. Check the host and port.'
    if (msg.includes('imap_tls_verify_failed')) return 'The mail server\'s TLS certificate could not be verified. Check the IMAP host name.'
    if (msg.includes('imap_ssl_error')) return 'SSL error — check that you are using the correct IMAP SSL port.'
    return msg
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    setError(null)
    setLoading(true)
    try {
      await createAgent({
        label: label.trim() || undefined,
        upstream_host: host,
        upstream_imap_port: imapPort,
        upstream_smtp_port: smtpPort,
        upstream_smtp_host: prefilledSmtpHost,
        upstream_user: user,
        upstream_password: password,
      })
      onSuccess()
    } catch (err) {
      setError(err instanceof Error ? mapError(err.message) : 'Connection failed.')
    } finally {
      setLoading(false)
    }
  }

  return (
    <form onSubmit={(e) => void handleSubmit(e)} className="space-y-4">
      <div>
        <label className="block text-sm font-medium text-fg-2 mb-1">
          Label <span className="text-fg-3 font-normal">(optional)</span>
        </label>
        <input type="text" value={label} onChange={(e) => setLabel(e.target.value)}
          className="w-full bg-surface-hi border border-edge rounded px-3 py-2 text-fg placeholder-fg-3 focus:outline-none focus:ring-2 focus:ring-accent"
          placeholder="e.g. Work, Personal" />
      </div>
      {!prefilledHost && (
        <div>
          <label className="block text-sm font-medium text-fg-2 mb-1">IMAP host</label>
          <input type="text" value={host} onChange={(e) => setHost(e.target.value)} required
            className="w-full bg-surface-hi border border-edge rounded px-3 py-2 text-fg placeholder-fg-3 focus:outline-none focus:ring-2 focus:ring-accent"
            placeholder="imap.example.com" />
        </div>
      )}
      {prefilledHost && (
        <p className="text-xs text-fg-3 font-mono bg-surface-hi border border-edge rounded px-3 py-2">
          IMAP: {prefilledHost}:{imapPort}
          {prefilledSmtpHost && <> · SMTP: {prefilledSmtpHost}:587</>}
        </p>
      )}
      {!prefilledHost && (
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
      )}
      <div>
        <label className="block text-sm font-medium text-fg-2 mb-1">Email address</label>
        <input type="email" value={user} onChange={(e) => setUser(e.target.value)} required
          className="w-full bg-surface-hi border border-edge rounded px-3 py-2 text-fg placeholder-fg-3 focus:outline-none focus:ring-2 focus:ring-accent"
          placeholder={placeholder} />
      </div>
      <div>
        <label className="block text-sm font-medium text-fg-2 mb-1">App-specific password</label>
        <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} required
          className="w-full bg-surface-hi border border-edge rounded px-3 py-2 text-fg placeholder-fg-3 focus:outline-none focus:ring-2 focus:ring-accent" />
        <p className="text-xs text-fg-3 mt-1">
          Use an app-specific password, not your account password.
        </p>
      </div>
      {error && <p className="text-red-400 text-sm">{error}</p>}
      <button type="submit" disabled={loading}
        className="w-full bg-accent hover:bg-accent-hi disabled:opacity-50 text-white dark:text-[#111c27] font-semibold py-2 px-4 rounded transition-colors">
        {loading ? 'Verifying connection…' : 'Connect'}
      </button>
      <button type="button" onClick={onSkip}
        className="w-full text-sm text-fg-3 hover:text-fg-2 transition-colors py-1">
        Skip for now — add later in Agents
      </button>
    </form>
  )
}

// ── Top-level wizard ──────────────────────────────────────────────────────

type Step = 'account' | 'provider' | 'connect'

export default function SetupView() {
  const navigate = useNavigate()
  const [step, setStep] = useState<Step>('account')
  const [provider, setProvider] = useState<Provider | null>(null)
  // Fail closed until config loads: assume no signup until the server confirms
  // otherwise, so we never flash a signup form on a private deployment.
  const [signupMode, setSignupMode] = useState<SignupMode>('closed')
  const [configLoaded, setConfigLoaded] = useState(false)

  useEffect(() => {
    let active = true
    void getConfig().then((cfg) => {
      if (active) {
        setSignupMode(cfg.signup_mode)
        setConfigLoaded(true)
      }
    })
    return () => { active = false }
  }, [])

  function handleProviderSelect(p: Provider) {
    setProvider(p)
    setStep('connect')
  }

  function finish() {
    navigate('/')
  }

  const STEP_LABELS: Record<Step, string> = {
    account: '1 of 3 — Create account',
    provider: '2 of 3 — Choose provider',
    connect: '3 of 3 — Connect email',
  }

  return (
    <div className="max-w-lg mx-auto mt-12">
      {/* Step indicator */}
      <div className="flex items-center gap-3 mb-6">
        {(['account', 'provider', 'connect'] as Step[]).map((s, i) => (
          <div key={s} className="flex items-center gap-2">
            <div className={`w-6 h-6 rounded-full flex items-center justify-center text-xs font-bold transition-colors ${
              step === s
                ? 'bg-accent text-white dark:text-[#111c27]'
                : (step === 'provider' && s === 'account') || (step === 'connect')
                  ? 'bg-emerald-500 text-white'
                  : 'bg-surface-hi text-fg-3'
            }`}>{i + 1}</div>
            {i < 2 && <div className={`h-px w-8 transition-colors ${
              (step === 'provider' && i === 0) || step === 'connect'
                ? 'bg-accent' : 'bg-edge'
            }`} />}
          </div>
        ))}
        <span className="text-xs text-fg-3 ml-1">{STEP_LABELS[step]}</span>
      </div>

      <div className="bg-surface rounded-xl border border-edge p-6">
        {step === 'account' && !configLoaded && (
          <p className="text-sm text-fg-3">Loading…</p>
        )}

        {step === 'account' && configLoaded && signupMode === 'closed' && (
          <ClosedSignupNotice />
        )}

        {step === 'account' && configLoaded && signupMode !== 'closed' && (
          <AccountStep mode={signupMode} onComplete={() => setStep('provider')} />
        )}

        {step === 'provider' && (
          <ProviderPicker
            onSelect={handleProviderSelect}
            heading="Connect your email"
            subheading="Choose which email service your AI agent should manage. You can add more providers later."
          />
        )}

        {step === 'connect' && provider === 'gmail' && (
          <>
            <h2 className="text-xl font-display font-black text-fg mb-1">Connect Gmail</h2>
            <p className="text-sm text-fg-3 mb-5">You'll be redirected to Google to authorise access.</p>
            <GmailForm onSkip={finish} />
          </>
        )}

        {step === 'connect' && provider === 'icloud' && (
          <>
            <h2 className="text-xl font-display font-black text-fg mb-1">Connect iCloud Mail</h2>
            <p className="text-sm text-fg-3 mb-1">Use an app-specific password from <a href="https://appleid.apple.com" target="_blank" rel="noreferrer" className="text-accent hover:underline">appleid.apple.com</a>.</p>
            <p className="text-xs text-fg-3 mb-5">Apple ID → Sign-In and Security → App-Specific Passwords → Generate.</p>
            <ImapForm
              prefilledHost="imap.mail.me.com"
              prefilledSmtpHost="smtp.mail.me.com"
              prefilledPort={993}
              placeholder="you@icloud.com"
              onSuccess={finish}
              onSkip={finish}
            />
          </>
        )}

        {step === 'connect' && provider === 'other' && (
          <>
            <h2 className="text-xl font-display font-black text-fg mb-1">Connect via IMAP</h2>
            <p className="text-sm text-fg-3 mb-5">Enter your email server details. Any IMAP/SMTP provider works.</p>
            <ImapForm
              prefilledHost=""
              prefilledPort={993}
              placeholder="you@example.com"
              onSuccess={finish}
              onSkip={finish}
            />
          </>
        )}

        {step === 'connect' && provider && (
          <button
            onClick={() => setStep('provider')}
            className="mt-4 text-xs text-fg-3 hover:text-fg-2 transition-colors"
          >
            ← Back to provider selection
          </button>
        )}
      </div>

      {step === 'provider' && (
        <p className="text-center text-sm text-fg-3 mt-4">
          <button onClick={finish} className="text-accent hover:underline">
            Skip — I'll add an agent later
          </button>
        </p>
      )}
    </div>
  )
}
