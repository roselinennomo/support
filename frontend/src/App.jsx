import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import {
  Activity, AlertCircle, ArrowDownLeft, ArrowLeft, ArrowUpRight, Check, CheckCheck,
  BrainCircuit, ChevronDown, ChevronUp, CircleHelp, Clock3, Database, FilePlus2,
  FileText, Headset, LifeBuoy, ListFilter, LoaderCircle, LogOut, MessageCircle,
  Paperclip, Plus, RefreshCw, Search, Send, ShieldCheck, Sparkles, Trash2, Users, UserRound,
  DollarSign, ClipboardList, BookOpen, ChevronLeft, ChevronRight, ChevronsLeft, ChevronsRight, HardDrive,
} from 'lucide-react'

const API = import.meta.env.VITE_API_BASE || '/api'
const NAVIGATION_STORAGE_KEY = 'oxe-navigation'
const NAVIGABLE_PAGES = new Set([
  'home', 'new', 'ticket', 'supervision', 'ai-routing', 'ai-traces', 'ai-costs', 'ai-consent',
  'help', 'rag', 'kb', 'company-docs', 'companies', 'access-requests', 'team', 'profile', 'ai-diagnostics', 'service', 'billing',
])
const statuses = {
  ai_triage: ['Diagnostic IA', 'amber'],
  new: ['Nouveau', 'blue'],
  in_progress: ['En cours', 'violet'],
  waiting_support: ['En attente support', 'blue'],
  waiting_customer: ['En attente client', 'amber'],
  escalated: ['Escaladé', 'red'],
  resolved: ['Résolu', 'green'],
  closed: ['Clôturé', 'gray'],
  rejected: ['Rejeté', 'red'],
}
const priorities = {
  low: ['P4 · Basse', 'gray'], medium: ['P3 · Normale', 'blue'], high: ['P2 · Haute', 'amber'], critical: ['P1 · Critique', 'red'],
}
const ticketTypes = { incident: 'Incident', problem: 'Problème', request: 'Demande' }

function readNavigationState() {
  try {
    const saved = JSON.parse(sessionStorage.getItem(NAVIGATION_STORAGE_KEY) || 'null')
    if (!saved || typeof saved !== 'object') return { page: 'home', ticketId: null, filter: 'all', search: '' }
    const ticketId = typeof saved.ticketId === 'string' ? saved.ticketId : null
    const page = NAVIGABLE_PAGES.has(saved.page) && (saved.page !== 'ticket' || ticketId)
      ? saved.page
      : 'home'
    return {
      page,
      ticketId,
      filter: typeof saved.filter === 'string' ? saved.filter : 'all',
      search: typeof saved.search === 'string' ? saved.search : '',
    }
  } catch {
    return { page: 'home', ticketId: null, filter: 'all', search: '' }
  }
}

async function request(path, { token, ...options } = {}) {
  const isFormData = options.body instanceof FormData
  const response = await fetch(`${API}${path}`, {
    ...options,
    headers: {
      ...(!isFormData ? { 'Content-Type': 'application/json' } : {}),
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...options?.headers,
    },
  })
  if (!response.ok) {
    let message = `Erreur ${response.status}`
    try {
      const data = await response.json()
      message = typeof data.detail === 'string' ? data.detail : message
    } catch { /* Response did not contain JSON. */ }
    throw new Error(message)
  }
  return response.status === 204 ? null : response.json()
}

function date(value) {
  return new Intl.DateTimeFormat('fr-FR', { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(value))
}

function Badge({ value, map = statuses }) {
  const [label, color] = map[value] || [value, 'gray']
  return <span className={`badge ${color}`}><span className="badge-dot" />{label}</span>
}

function Avatar({ name, size = 'normal' }) {
  return <span className={`avatar ${size}`}>{name?.split(/\s+/).slice(0, 2).map((part) => part[0]).join('').toUpperCase() || '?'}</span>
}

function MessageBody({ body, isMarkdown }) {
  if (!isMarkdown) return <p>{body}</p>
  return <div className="message-markdown"><ReactMarkdown remarkPlugins={[remarkGfm]}>{body}</ReactMarkdown></div>
}

function pageWindow(page, pageCount) {
  if (pageCount <= 7) return Array.from({ length: pageCount }, (_, index) => index + 1)
  const pages = new Set([1, pageCount, page - 1, page, page + 1])
  if (page <= 3) [2, 3, 4].forEach((value) => pages.add(value))
  if (page >= pageCount - 2) [pageCount - 1, pageCount - 2, pageCount - 3].forEach((value) => pages.add(value))
  const sorted = [...pages].filter((value) => value >= 1 && value <= pageCount).sort((a, b) => a - b)
  const result = []
  sorted.forEach((value, index) => {
    if (index && value - sorted[index - 1] > 1) result.push(`gap-${value}`)
    result.push(value)
  })
  return result
}

function storedPageSize(key, fallback) {
  try {
    const value = Number(localStorage.getItem(`oxe-page-size:${key}`))
    return value > 0 ? value : fallback
  } catch { return fallback }
}

function rememberPageSize(key, size) {
  try { localStorage.setItem(`oxe-page-size:${key}`, String(size)) } catch { /* stockage indisponible */ }
}

/** Pagination côté client d'une liste déjà chargée. `resetKey` ramène à la page 1 quand le filtre change. */
function usePagination(items, { storageKey = '', initialSize = 25, resetKey = '' } = {}) {
  const [pageSize, setPageSizeState] = useState(() => (storageKey ? storedPageSize(storageKey, initialSize) : initialSize))
  const [page, setPage] = useState(1)
  const pageCount = Math.max(1, Math.ceil(items.length / pageSize))
  useEffect(() => { setPage(1) }, [resetKey, pageSize])
  const current = Math.min(page, pageCount)
  const start = (current - 1) * pageSize
  const setPageSize = (size) => {
    setPageSizeState(size)
    if (storageKey) rememberPageSize(storageKey, size)
  }
  return { items: items.slice(start, start + pageSize), page: current, pageSize, total: items.length, setPage, setPageSize }
}

/** Barre de pagination commune (client ou serveur) : plage affichée, pages numérotées, taille de page. */
function Pagination({ page, pageSize, total, setPage, setPageSize, sizes = [10, 25, 50, 100], label = 'éléments', compact = false, disabled = false }) {
  const pageCount = Math.max(1, Math.ceil(total / pageSize))
  if (total <= Math.min(...sizes, pageSize)) return null
  const from = total === 0 ? 0 : (page - 1) * pageSize + 1
  const to = Math.min(page * pageSize, total)
  const go = (target) => setPage(Math.min(pageCount, Math.max(1, target)))
  const sizeOptions = [...new Set([...sizes, pageSize])].sort((a, b) => a - b)
  return <nav className={`pager ${compact ? 'compact' : ''}`} aria-label="Pagination">
    <span className="pager-range">{from}–{to} sur {total} {label}</span>
    <div className="pager-controls">
      <button type="button" aria-label="Première page" disabled={disabled || page <= 1} onClick={() => go(1)}><ChevronsLeft size={14} /></button>
      <button type="button" aria-label="Page précédente" disabled={disabled || page <= 1} onClick={() => go(page - 1)}><ChevronLeft size={14} /></button>
      {compact
        ? <span className="pager-compact">Page {page}/{pageCount}</span>
        : pageWindow(page, pageCount).map((item) => typeof item === 'number'
          ? <button key={item} type="button" className="pager-page" aria-label={`Page ${item}`} aria-current={item === page ? 'page' : undefined} disabled={disabled} onClick={() => go(item)}>{item}</button>
          : <span key={item} className="pager-gap">…</span>)}
      <button type="button" aria-label="Page suivante" disabled={disabled || page >= pageCount} onClick={() => go(page + 1)}><ChevronRight size={14} /></button>
      <button type="button" aria-label="Dernière page" disabled={disabled || page >= pageCount} onClick={() => go(pageCount)}><ChevronsRight size={14} /></button>
    </div>
    {!compact && setPageSize && <div className="pager-size"><span>Par page</span><select aria-label="Éléments par page" value={pageSize} disabled={disabled} onChange={(event) => setPageSize(Number(event.target.value))}>{sizeOptions.map((size) => <option key={size} value={size}>{size}</option>)}</select></div>}
  </nav>
}

function AuthScreen({ onAuth }) {
  const [register, setRegister] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [confirmation, setConfirmation] = useState('')
  const [form, setForm] = useState({
    full_name: '',
    email: '',
    password: '',
    company_code: '',
    account_type: 'company',
    ai_processing_consent: true,
    ai_trace_consent: true,
  })

  async function submit(event) {
    event.preventDefault()
    setBusy(true)
    setError('')
    setConfirmation('')
    try {
      const result = await request(register ? '/auth/register' : '/auth/login', {
        method: 'POST',
        body: JSON.stringify(form),
      })
      if (register) setConfirmation(result.message)
      else onAuth(result)
    } catch (err) {
      setError(err.message)
      return
    } finally {
      setBusy(false)
    }
  }

  return (
    <main className="auth-shell">
      <div className="auth-art">
        <div className="brand light"><span className="brand-icon"><Headset size={21} /></span> OXE <b>SUPPORT</b></div>
        <div className="art-copy">
          <span className="eyebrow light-eyebrow"><span className="live-dot" /> ASSISTANCE TECHNIQUE</span>
          <h1>Votre téléphonie,<br />entre de bonnes mains.</h1>
          <p>Un support expert pour vos solutions Alcatel-Lucent. Suivez vos demandes, échangez avec nos équipes et avancez sereinement.</p>
        </div>
        <div className="art-footer"><span><ShieldCheck size={16} /> Support sécurisé</span><span>Alcatel-Lucent OXE</span></div>
      </div>
      <div className="auth-panel">
        <div className="mobile-brand"><Headset size={19} /> OXE SUPPORT</div>
        <div className="auth-card">
          <span className="eyebrow">ESPACE CLIENT</span>
          <h2>{register ? 'Demander un accès' : 'Ravi de vous revoir'}</h2>
          <p className="muted">{register ? 'Demandez un accès avec le code communiqué par votre support. Votre compte sera activé après validation.' : 'Connectez-vous pour accéder à vos demandes de support.'}</p>
          {error && <div className="alert error"><AlertCircle size={17} />{error}</div>}
          {confirmation && <div className="alert success"><Check size={16} />{confirmation}</div>}
          <form onSubmit={submit} className="form-stack">
            {register && <label>Nom complet<input required minLength="2" autoComplete="name" placeholder="Ex. Camille Martin" value={form.full_name} onChange={(e) => setForm({ ...form, full_name: e.target.value })} /></label>}
            {register && <label className="account-type-check"><input type="checkbox" checked={form.account_type === 'independent'} onChange={(e) => setForm({ ...form, account_type: e.target.checked ? 'independent' : 'company', company_code: '' })} /><span>Je suis un client indépendant (sans entreprise) : mon espace et ma facturation me sont propres.</span></label>}
            {register && form.account_type === 'company' && <label>Code d’accès entreprise<input required autoComplete="off" placeholder="Code fourni par le support" value={form.company_code} onChange={(e) => setForm({ ...form, company_code: e.target.value })} /></label>}
            {register && <div className="registration-consents">
              <label><input type="checkbox" checked={form.ai_processing_consent} onChange={(event) => setForm({ ...form, ai_processing_consent: event.target.checked })} /><span>J’accepte le traitement de mes tickets par les LLM pour le diagnostic (activé par défaut).</span></label>
              <label><input type="checkbox" checked={form.ai_trace_consent} onChange={(event) => setForm({ ...form, ai_trace_consent: event.target.checked })} /><span>J’accepte la conservation des prompts/réponses pour évaluation pendant 30 jours maximum (activé par défaut).</span></label>
            </div>}
            <label>Adresse e-mail<input required type="email" autoComplete="email" placeholder="vous@entreprise.fr" value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} /></label>
            <label>Mot de passe<input required type="password" minLength={register ? 10 : 1} autoComplete={register ? 'new-password' : 'current-password'} placeholder={register ? '10 caractères minimum' : 'Votre mot de passe'} value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} /></label>
            <button className="button primary full" disabled={busy}>{busy ? register ? 'Envoi…' : 'Connexion…' : register ? 'Envoyer la demande' : 'Se connecter'}<ArrowUpRight size={17} /></button>
          </form>
          <div className="auth-switch">{register ? 'Vous avez déjà un compte ?' : 'Votre entreprise est déjà cliente ?'} <button onClick={() => { setRegister(!register); setError(''); setConfirmation('') }}>{register ? 'Se connecter' : 'Demander un accès'}</button></div>
          <div className="auth-security"><ShieldCheck size={16} /> Les demandes d’accès sont vérifiées par l’équipe support avant toute création d’incident.</div>
        </div>
        <span className="auth-copyright">© 2026 OXE Support · Assistance technique</span>
      </div>
    </main>
  )
}

function AIConsentPage({ user, token, onSaved }) {
  const [processing, setProcessing] = useState(user.ai_processing_consent !== false)
  const [traces, setTraces] = useState(user.ai_trace_consent !== false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')

  async function save(event) {
    event.preventDefault()
    setSaving(true)
    setError('')
    setNotice('')
    try {
      const updated = await request('/auth/ai-consent', {
        token,
        method: 'PUT',
        body: JSON.stringify({
          ai_processing_consent: processing,
          ai_trace_consent: traces,
        }),
      })
      onSaved(updated)
      setNotice(!traces && user.ai_trace_consent
        ? 'Vos choix sont enregistrés. Les contenus des traces historiques ont été supprimés.'
        : 'Vos choix de confidentialité sont enregistrés.')
    } catch (err) {
      setError(err.message)
    } finally {
      setSaving(false)
    }
  }

  return <div className="ai-consent-page">
    <div className="welcome-row"><div><span className="eyebrow">CONFIDENTIALITÉ</span><h1>Consentements IA</h1><p>Vous pouvez modifier vos choix à tout moment. Les deux consentements sont activés par défaut.</p></div></div>
    {error && <div className="alert error page-alert">{error}</div>}
    {notice && <div className="alert success page-alert">{notice}</div>}
    <form onSubmit={save}>
      <section className="panel consent-panel">
        <div className="panel-head"><div><h2>Traitement IA de vos demandes</h2><p>Autorise l’envoi du contenu des tickets aux modèles IA configurés afin de proposer un diagnostic et une assistance.</p></div><BrainCircuit size={19} className="muted-icon" /></div>
        <label className="consent-choice"><input type="checkbox" checked={processing} onChange={(event) => setProcessing(event.target.checked)} /><span><strong>J’accepte le traitement IA du contenu de mes tickets</strong><small>Si vous désactivez cette option, les réponses IA et l’assistance IA sur vos tickets sont arrêtées. L’équipe support reste disponible.</small></span></label>
      </section>
      <section className="panel consent-panel">
        <div className="panel-head"><div><h2>Conservation pour évaluation</h2><p>Autorise les administrateurs à consulter les prompts et réponses pour évaluer la qualité des modèles.</p></div><ClipboardList size={19} className="muted-icon" /></div>
        <label className="consent-choice"><input type="checkbox" checked={traces} onChange={(event) => setTraces(event.target.checked)} /><span><strong>J’accepte la conservation des échanges IA pour leur évaluation</strong><small>Les contenus sont conservés au maximum 30 jours. En désactivant cette option, les prompts et réponses déjà conservés sont supprimés; les données de consommation nécessaires au suivi des coûts restent sans contenu conversationnel.</small></span></label>
      </section>
      <button className="button primary" disabled={saving}><Check size={16} />{saving ? 'Enregistrement…' : 'Enregistrer mes choix'}</button>
    </form>
  </div>
}

function ProfilePage({ user, companies, token, onAuthChanged }) {
  const [passwords, setPasswords] = useState({ current: '', next: '', confirm: '' })
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const roles = { customer: 'Client', agent: 'Agent support', admin: 'Administrateur' }
  const accountStatuses = { approved: 'Actif', pending: 'En attente', rejected: 'Refusé' }
  const assignedCompanies = companies
    .filter((company) => user.role === 'agent' && user.company_ids?.includes(company.id))
    .map((company) => company.name)

  async function changePassword(event) {
    event.preventDefault()
    setSaving(true)
    setError('')
    setNotice('')
    if (passwords.next !== passwords.confirm) {
      setError('La confirmation ne correspond pas au nouveau mot de passe.')
      setSaving(false)
      return
    }
    try {
      const updatedAuth = await request('/auth/password', {
        token,
        method: 'PUT',
        body: JSON.stringify({
          current_password: passwords.current,
          new_password: passwords.next,
        }),
      })
      onAuthChanged(updatedAuth)
      setPasswords({ current: '', next: '', confirm: '' })
      setNotice('Mot de passe modifié. Les autres sessions ont été déconnectées.')
    } catch (err) {
      setError(err.message)
    } finally {
      setSaving(false)
    }
  }

  return <div className="profile-page">
    <div className="welcome-row"><div><span className="eyebrow">MON COMPTE</span><h1>Mon profil</h1><p>Consultez vos informations et gérez votre accès.</p></div></div>
    {error && <div className="alert error page-alert">{error}</div>}
    {notice && <div className="alert success page-alert">{notice}</div>}
    <section className="panel profile-info-panel">
      <div className="panel-head"><div><h2>Informations du compte</h2><p>Ces informations sont utilisées pour identifier votre compte sur la plateforme.</p></div><UserRound size={19} className="muted-icon" /></div>
      <dl className="profile-info-grid">
        <div><dt>Nom complet</dt><dd>{user.full_name}</dd></div>
        <div><dt>Adresse e-mail</dt><dd>{user.email}</dd></div>
        <div><dt>Profil</dt><dd>{roles[user.role] || user.role}</dd></div>
        <div><dt>État du compte</dt><dd>{accountStatuses[user.account_status] || user.account_status}</dd></div>
        {user.role === 'customer' && <div><dt>Entreprise</dt><dd>{user.company_name || 'Aucune entreprise associée'}</dd></div>}
        {user.role === 'agent' && <div className="profile-company-field"><dt>Entreprises associées</dt><dd>{assignedCompanies.length ? assignedCompanies.join(', ') : 'Aucune entreprise associée'}</dd></div>}
        {user.role === 'admin' && <div><dt>Périmètre</dt><dd>Accès à l’ensemble des entreprises</dd></div>}
        {user.role === 'customer' && <>
          <div><dt>Analyse IA des tickets</dt><dd>{user.ai_processing_consent ? 'Autorisée' : 'Désactivée'}</dd></div>
          <div><dt>Conservation des échanges IA</dt><dd>{user.ai_trace_consent ? 'Autorisée' : 'Désactivée'}</dd></div>
        </>}
      </dl>
    </section>
    <form className="panel profile-password-panel" onSubmit={changePassword}>
      <div className="panel-head"><div><h2>Modifier le mot de passe</h2><p>Choisissez un mot de passe d’au moins 10 caractères. Les autres sessions seront déconnectées.</p></div><ShieldCheck size={19} className="muted-icon" /></div>
      <div className="profile-password-fields">
        <label>Mot de passe actuel<input required type="password" autoComplete="current-password" value={passwords.current} onChange={(event) => setPasswords({ ...passwords, current: event.target.value })} /></label>
        <label>Nouveau mot de passe<input required minLength="10" maxLength="128" type="password" autoComplete="new-password" value={passwords.next} onChange={(event) => setPasswords({ ...passwords, next: event.target.value })} /></label>
        <label>Confirmer le nouveau mot de passe<input required minLength="10" maxLength="128" type="password" autoComplete="new-password" value={passwords.confirm} onChange={(event) => setPasswords({ ...passwords, confirm: event.target.value })} /></label>
      </div>
      <div className="profile-password-actions"><button className="button primary" disabled={saving}>{saving ? 'Modification…' : 'Modifier mon mot de passe'}</button></div>
    </form>
  </div>
}

function AIDiagnosticsPage({ token }) {
  const [diagnostics, setDiagnostics] = useState(null)
  const [reference, setReference] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  const load = useCallback(async (ticketReference = '') => {
    setLoading(true)
    setError('')
    try {
      const query = ticketReference.trim()
        ? `?${new URLSearchParams({ reference: ticketReference.trim() })}`
        : ''
      setDiagnostics(await request(`/admin/ai/diagnostics${query}`, { token }))
      if (ticketReference.trim()) setReference(ticketReference.trim().toUpperCase())
    } catch (err) {
      setError(err.message)
      if (ticketReference.trim()) setDiagnostics((current) => current ? { ...current, ticket: null } : current)
    } finally {
      setLoading(false)
    }
  }, [token])

  useEffect(() => { load() }, [load])

  function statusLabel(status) {
    return ({ healthy: 'Disponible', responded: 'A répondu', configured: 'Configuré', warning: 'À vérifier', down: 'Indisponible', disabled: 'Désactivé', unconfigured: 'Non configuré' })[status] || status
  }

  return <div className="ai-diagnostics-page">
    <div className="welcome-row">
      <div><span className="eyebrow">OUTILS ADMINISTRATEUR</span><h1>Diagnostic IA</h1><p>Comprenez les erreurs de routage et de fournisseur sans consulter les journaux serveur.</p></div>
      <button className="button secondary" onClick={() => load(reference)} disabled={loading}><RefreshCw size={16} />{loading ? 'Vérification…' : 'Actualiser'}</button>
    </div>
    {error && <div className="alert error page-alert"><AlertCircle size={16} />{error}</div>}
    <section className="ai-diagnostic-provider-grid">
      {(diagnostics?.providers || []).map((provider) => <article className="panel ai-diagnostic-provider" key={provider.id}>
        <div className="ai-diagnostic-provider-heading"><div><span className="eyebrow">FOURNISSEUR</span><h2>{provider.label}</h2></div><span className={`ai-diagnostic-status ${provider.status}`}>{statusLabel(provider.status)}</span></div>
        <p>{provider.last_success_at
          ? `Dernier appel réussi le ${date(provider.last_success_at)} avec ${provider.last_success_model}.`
          : provider.error || (provider.id === 'ollama'
            ? `${provider.available_models.length} modèle(s) disponible(s) · modèle par défaut ${provider.default_model}`
            : `Clé configurée pour ${provider.default_model}; aucun appel réussi n’est encore enregistré.`)}
          {provider.last_failure_at && ` Dernier échec : ${date(provider.last_failure_at)}${provider.last_failure_message ? ` — ${provider.last_failure_message}` : ''}`}
        </p>
        {provider.available_models.length > 0 && <div className="ai-diagnostic-models">{provider.available_models.map((model) => <span key={model}>{model}</span>)}</div>}
      </article>)}
    </section>

    {diagnostics?.embedding && <section className="ai-diagnostic-provider-grid">
      <article className="panel ai-diagnostic-provider">
        <div className="ai-diagnostic-provider-heading"><div><span className="eyebrow">EMBEDDINGS RAG</span><h2>Ollama (embeddings)</h2></div><span className={`ai-diagnostic-status ${diagnostics.embedding.status}`}>{statusLabel(diagnostics.embedding.status)}</span></div>
        <p>{diagnostics.embedding.error || `Modèle actif ${diagnostics.embedding.model} · ${diagnostics.embedding.dimension} dimensions · ${diagnostics.embedding.dedicated_instance ? 'instance dédiée' : 'instance partagée avec la génération'}${diagnostics.embedding.status === 'warning' ? ' — modèle actif non téléchargé' : ''}`}</p>
        <div className="ai-diagnostic-models">{diagnostics.embedding.options.map((option) => <span key={option.id}>{option.label}{option.selected ? ' · actif' : ''}{option.installed ? '' : ' · non téléchargé'}</span>)}</div>
      </article>
      {diagnostics.rag && <article className="panel ai-diagnostic-provider">
        <div className="ai-diagnostic-provider-heading"><div><span className="eyebrow">BASE VECTORIELLE</span><h2>Qdrant</h2></div><span className={`ai-diagnostic-status ${diagnostics.rag.status}`}>{statusLabel(diagnostics.rag.status)}</span></div>
        <p>{diagnostics.rag.error || `Collection ${diagnostics.rag.collection} ${diagnostics.rag.collection_exists ? 'présente.' : 'absente : lancez l’indexation du corpus.'}`}</p>
      </article>}
    </section>}

    <section className="panel ai-diagnostic-routes">
      <div className="panel-head"><div><h2>Routage en vigueur</h2><p>{diagnostics?.routing_enabled ? 'Le routage IA global est activé.' : 'Le routage IA global est désactivé.'}</p></div><BrainCircuit size={19} className="muted-icon" /></div>
      <div className="ai-diagnostic-route-list">
        {(diagnostics?.routes || []).map((route) => <div className={`ai-diagnostic-route ${route.enabled ? '' : 'disabled'}`} key={`${route.task}-${route.role}`}>
          <div><strong>{route.task_label}</strong><span>{route.role_label}</span></div>
          <div className="ai-diagnostic-route-models">{route.models.length
            ? route.models.map((model, index) => <span key={`${model.provider}-${model.model}`}><b>{index + 1}.</b> {model.provider}/{model.model}{model.provider === 'ollama' ? (model.think ? ' · think' : ' · sans think') : ''} · {model.timeout_seconds || 120}s</span>)
            : <span>Aucun modèle affecté</span>}</div>
          <span className={`ai-diagnostic-status ${route.enabled ? 'healthy' : 'disabled'}`}>{route.enabled ? 'Actif' : 'Inactif'}</span>
        </div>)}
      </div>
    </section>

    <section className="panel ai-diagnostic-ticket-search">
      <div className="panel-head"><div><h2>Diagnostiquer un ticket</h2><p>Recherche par référence : état IA, erreurs, événements et appels réussis (métadonnées uniquement).</p></div><Search size={19} className="muted-icon" /></div>
      <form className="ai-diagnostic-search" onSubmit={(event) => { event.preventDefault(); load(reference) }}>
        <input aria-label="Référence du ticket" value={reference} onChange={(event) => setReference(event.target.value)} placeholder="Ex. OXE-854F2320" />
        <button className="button primary" disabled={loading || !reference.trim()}><Search size={15} />Analyser</button>
      </form>
      {diagnostics?.ticket && <div className="ai-diagnostic-ticket">
        <div className="ai-diagnostic-ticket-heading">
          <div><span className="eyebrow">{diagnostics.ticket.reference}</span><h3>{diagnostics.ticket.title}</h3></div>
          <div className="ai-diagnostic-ticket-tags"><span>Ticket : {diagnostics.ticket.status}</span><span>IA : {diagnostics.ticket.ai_status}</span></div>
        </div>
        {diagnostics.ticket.ai_error && <div className="ai-diagnostic-error"><AlertCircle size={16} /><div><strong>Dernière erreur enregistrée</strong><span>{diagnostics.ticket.ai_error}</span></div></div>}
        <h4>Historique des exécutions réussies</h4>
        {diagnostics.ticket.executions.length
          ? <div className="ai-diagnostic-table-wrap"><table className="ai-diagnostic-table"><thead><tr><th>Date</th><th>Tâche</th><th>Fournisseur / modèle</th><th>Tokens entrée/sortie</th><th>Durée</th></tr></thead><tbody>
            {diagnostics.ticket.executions.map((execution, index) => <tr key={`${execution.created_at}-${index}`}><td>{date(execution.created_at)}</td><td>{execution.task}</td><td>{execution.provider}/{execution.model}</td><td>{execution.input_tokens} / {execution.output_tokens}</td><td>{execution.duration_ms} ms</td></tr>)}
          </tbody></table></div>
          : <p className="ai-diagnostic-empty">Aucun appel IA réussi n’est enregistré pour ce ticket.</p>}
        <h4>Événements de diagnostic</h4>
        {diagnostics.ticket.events.length
          ? <div className="ai-diagnostic-events">{diagnostics.ticket.events.map((event, index) => <div key={`${event.created_at}-${index}`}><span className={`ai-diagnostic-event-dot ${event.event_type.includes('failed') ? 'failed' : ''}`} /><time>{date(event.created_at)}</time><strong>{event.event_type}{event.provider ? ` · ${event.provider}/${event.model}` : ''}{event.task ? ` · ${event.task}` : ''}</strong>{event.message && <p>{event.message}</p>}</div>)}</div>
          : <p className="ai-diagnostic-empty">Aucun événement IA n’est enregistré pour ce ticket.</p>}
      </div>}
    </section>

    <section className="panel ai-diagnostic-failures">
      <div className="panel-head"><div><h2>Échecs récents</h2><p>Derniers échecs de diagnostic client et de résumé de transfert.</p></div><AlertCircle size={19} className="muted-icon" /></div>
      <div className="ai-diagnostic-failure-list">
        {(diagnostics?.recent_failures || []).map((failure, index) => <button className="ai-diagnostic-failure" key={`${failure.reference}-${failure.created_at}-${index}`} onClick={() => load(failure.reference)}>
          <span><strong>{failure.reference}</strong><small>{failure.title}</small></span><span className="ai-diagnostic-failure-message">{failure.provider ? `${failure.provider}/${failure.model} · ` : ''}{failure.task ? `${failure.task} · ` : ''}{failure.message || failure.event_type}</span><time>{date(failure.created_at)}</time><ArrowUpRight size={15} />
        </button>)}
        {diagnostics && diagnostics.recent_failures.length === 0 && <p className="ai-diagnostic-empty">Aucun échec récent enregistré.</p>}
      </div>
    </section>
  </div>
}

function ServicePortalPage({ token, user, companies }) {
  const currentMonth = new Date().toISOString().slice(0, 7)
  const [selectedCompany, setSelectedCompany] = useState(user.company_id || companies[0]?.id || '')
  const [policies, setPolicies] = useState([])
  const [reports, setReports] = useState([])
  const [invoices, setInvoices] = useState([])
  const [summary, setSummary] = useState(null)
  const [period, setPeriod] = useState({ start: `${currentMonth}-01`, end: new Date().toISOString().slice(0, 10), invoice: currentMonth })
  const [proposal, setProposal] = useState({ ticket_type: 'incident', priority: 'p3', workaround_hours: 2, resolution_hours: 24, support_units_per_hour: 1, customer_penalty_units: 0.5 })
  const [reviewNotes, setReviewNotes] = useState({})
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const isCustomer = user.role === 'customer'

  const load = useCallback(async () => {
    setError('')
    try {
      const companyQuery = !isCustomer && selectedCompany ? `?${new URLSearchParams({ company_id: selectedCompany })}` : ''
      const [slaRows, reportRows, invoiceRows] = await Promise.all([
        request(`/service/sla${companyQuery}`, { token }),
        request('/service/reports', { token }),
        request(`/billing/invoices${companyQuery}`, { token }),
      ])
      setPolicies(slaRows)
      setReports(selectedCompany ? reportRows.filter((job) => job.company_id === selectedCompany) : reportRows)
      setInvoices(invoiceRows)
      if (selectedCompany) {
        const [year, month] = period.invoice.split('-').map(Number)
        setSummary(await request(`/billing/summary?${new URLSearchParams({ company_id: selectedCompany, year, month })}`, { token }))
      }
    } catch (err) {
      setError(err.message)
    }
  }, [token, user.role, selectedCompany, period.invoice])

  useEffect(() => {
    if (!selectedCompany && companies.length && user.role !== 'customer') setSelectedCompany(companies[0].id)
  }, [companies, selectedCompany, user.role])

  useEffect(() => { load() }, [load])

  useEffect(() => {
    if (!reports.some((job) => job.status === 'pending' || job.status === 'processing')) return undefined
    const timer = window.setInterval(load, 2500)
    return () => window.clearInterval(timer)
  }, [reports, load])

  async function submitProposal(event) {
    event.preventDefault()
    setBusy(true)
    setError('')
    setNotice('')
    try {
      await request('/service/sla/proposals', { token, method: 'POST', body: JSON.stringify(proposal) })
      setNotice('Proposition SLA transmise à l’agent de votre entreprise pour validation.')
      await load()
    } catch (err) { setError(err.message) } finally { setBusy(false) }
  }

  async function reviewPolicy(policy, decision) {
    setBusy(true)
    setError('')
    try {
      await request(`/service/sla/${policy.id}`, {
        token,
        method: 'PATCH',
        body: JSON.stringify({ decision, note: reviewNotes[policy.id] || '' }),
      })
      await load()
    } catch (err) { setError(err.message) } finally { setBusy(false) }
  }

  async function generateReport(event) {
    event.preventDefault()
    setBusy(true)
    setError('')
    setNotice('')
    try {
      await request('/service/reports', {
        token,
        method: 'POST',
        body: JSON.stringify({
          period_start: period.start,
          period_end: period.end,
          ...(!isCustomer ? { company_id: selectedCompany } : {}),
        }),
      })
      setNotice('Génération du rapport lancée. Actualisez la liste dans quelques instants.')
      await load()
    } catch (err) { setError(err.message) } finally { setBusy(false) }
  }

  async function generateInvoice(event) {
    event.preventDefault()
    const [year, month] = period.invoice.split('-').map(Number)
    setBusy(true)
    setError('')
    try {
      const invoice = await request(`/billing/invoices/${year}/${month}?${new URLSearchParams({ company_id: selectedCompany })}`, {
        token,
        method: 'POST',
      })
      if (!invoice?.id) throw new Error('La facture a été créée, mais son identifiant est absent de la réponse.')
      setNotice('Facture créée et disponible au téléchargement.')
      await load()
    } catch (err) { setError(err.message) } finally { setBusy(false) }
  }

  const visiblePolicies = policies.filter((policy) => !selectedCompany || policy.company_id === selectedCompany)
  const policyPager = usePagination(visiblePolicies, { storageKey: 'sla', initialSize: 10, resetKey: selectedCompany })
  const reportPager = usePagination(reports, { storageKey: 'reports', initialSize: 5 })
  const invoicePager = usePagination(invoices, { storageKey: 'invoices', initialSize: 5 })

  return <div className="team-page service-portal-page">
    <div className="welcome-row"><div><span className="eyebrow">CONTRATS & SUIVI</span><h1>SLA, rapports et factures</h1><p>Propositions de niveaux de service, suivi des unités et documents de période.</p></div><button className="button secondary" onClick={load} disabled={busy}><RefreshCw size={15} />Actualiser</button></div>
    {error && <div className="alert error page-alert">{error}</div>}
    {notice && <div className="alert success page-alert">{notice}</div>}
    {!isCustomer && <label className="service-company-select">Entreprise<select value={selectedCompany} onChange={(event) => setSelectedCompany(event.target.value)}>{companies.map((company) => <option value={company.id} key={company.id}>{company.name}</option>)}</select></label>}
    <section className="panel">
      <div className="panel-head"><div><h2>Accords de niveau de service</h2><p>Les clients proposent les délais et pénalités en unités U; un agent de l’entreprise valide chaque version.</p></div><Clock3 size={19} className="muted-icon" /></div>
      {policies.length ? <><div className="sla-policy-list">{policyPager.items.map((policy) => <article className="sla-policy-row" key={policy.id}>
        <div className="sla-policy-title"><strong>{ticketTypes[policy.ticket_type]} · {policy.priority.toUpperCase()}</strong><span className={`sla-policy-state ${policy.status}`}>{({ pending: 'À valider', approved: 'En vigueur', rejected: 'Refusé', superseded: 'Remplacé' })[policy.status]}</span></div>
        <p>Contournement : <b>{policy.workaround_hours} h</b> · Résolution : <b>{policy.resolution_hours} h</b></p>
        <p>Pénalité support : <b>{policy.support_units_per_hour} U/h</b> · Constat client : <b>{policy.customer_penalty_units} U</b></p>
        {!isCustomer && policy.status === 'pending' && <div className="sla-review-controls"><input placeholder="Note de validation (facultative)" value={reviewNotes[policy.id] || ''} onChange={(event) => setReviewNotes({ ...reviewNotes, [policy.id]: event.target.value })} /><button className="button primary" disabled={busy} onClick={() => reviewPolicy(policy, 'approve')}>Valider</button><button className="button secondary" disabled={busy} onClick={() => reviewPolicy(policy, 'reject')}>Refuser</button></div>}
      </article>)}</div><Pagination {...policyPager} sizes={[5, 10, 25]} label="accords" /></> : <div className="empty-state compact">Aucun SLA proposé. Les tickets restent créables mais sans échéance SLA jusqu’à l’approbation d’un accord.</div>}
      {isCustomer && <form className="sla-proposal-form" onSubmit={submitProposal}>
        <h3>Proposer un SLA</h3>
        <div className="sla-proposal-fields">
          <label>Type<select value={proposal.ticket_type} onChange={(event) => setProposal({ ...proposal, ticket_type: event.target.value })}>{Object.entries(ticketTypes).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></label>
          <label>Priorité<select value={proposal.priority} onChange={(event) => setProposal({ ...proposal, priority: event.target.value })}><option value="p1">P1 · Critique</option><option value="p2">P2 · Haute</option><option value="p3">P3 · Normale</option><option value="p4">P4 · Basse</option></select></label>
          <label>Contournement (heures)<input type="number" min="1" max="2160" value={proposal.workaround_hours} onChange={(event) => setProposal({ ...proposal, workaround_hours: Number(event.target.value) })} /></label>
          <label>Résolution cible (heures)<input type="number" min="1" max="8760" value={proposal.resolution_hours} onChange={(event) => setProposal({ ...proposal, resolution_hours: Number(event.target.value) })} /></label>
          <label>Pénalité support (U/heure)<input type="number" min="0" step="0.1" value={proposal.support_units_per_hour} onChange={(event) => setProposal({ ...proposal, support_units_per_hour: Number(event.target.value) })} /></label>
          <label>Pénalité client (U)<input type="number" min="0" step="0.1" value={proposal.customer_penalty_units} onChange={(event) => setProposal({ ...proposal, customer_penalty_units: Number(event.target.value) })} /></label>
        </div>
        <button className="button primary" disabled={busy}>Soumettre la proposition</button>
      </form>}
    </section>

    <section className="service-finance-grid">
      <article className="panel service-finance-card">
        <div className="panel-head"><div><h2>Rapport de période</h2><p>PDF avec indicateurs, détail des tickets et unités dues de part et d’autre.</p></div><FileText size={18} className="muted-icon" /></div>
        <form className="service-period-form" onSubmit={generateReport}><label>Du<input type="date" required value={period.start} onChange={(event) => setPeriod({ ...period, start: event.target.value })} /></label><label>Au<input type="date" required value={period.end} onChange={(event) => setPeriod({ ...period, end: event.target.value })} /></label><button className="button primary" disabled={busy || (!isCustomer && !selectedCompany)}><FileText size={15} />Générer le PDF</button></form>
        <div className="service-document-list">{reportPager.items.map((job) => <div className="service-document-row" key={job.id}><span><strong>Rapport</strong><small>{job.period_start.slice(0, 10)} – {job.period_end.slice(0, 10)}</small></span><span className={`sla-policy-state ${job.status}`}>{job.status === 'completed' ? 'Prêt' : job.status === 'failed' ? 'Échec' : 'En cours'}</span>{job.status === 'completed' && <button className="button secondary" onClick={() => downloadPdf(`/service/reports/${job.id}/download`, token, `rapport-${job.id}.pdf`).catch((err) => setError(err.message))}>Télécharger</button>}{job.error && <small className="error-text">{job.error}</small>}</div>)}</div>
        <Pagination {...reportPager} sizes={[5, 10, 25]} compact label="rapports" />
      </article>
      <article className="panel service-finance-card">
        <div className="panel-head"><div><h2>Coût et factures</h2><p>Montant calculé selon le paramétrage financier de l’entreprise.</p></div><DollarSign size={18} className="muted-icon" /></div>
        <form className="service-invoice-form" onSubmit={generateInvoice}><label>Période<input type="month" value={period.invoice} onChange={(event) => setPeriod({ ...period, invoice: event.target.value })} /></label>{!isCustomer && <button className="button primary" disabled={busy || !selectedCompany}>Générer / ouvrir la facture</button>}</form>
        {summary && <div className="service-billing-summary"><div><span>Tickets</span><strong>{summary.ticket_count}</strong></div><div><span>Montant estimé TTC</span><strong>{(summary.total_cents / 100).toFixed(2)} {summary.currency}</strong></div><div><span>Paiement</span><strong>{summary.payment_mode === 'prepaid' ? 'Prépayé' : 'Postpayé'}</strong></div><div><span>Crédits tickets restants</span><strong>{summary.prepaid_ticket_credits}</strong></div><div><span>Crédits mois restants</span><strong>{summary.prepaid_month_credits}</strong></div></div>}
        <div className="service-document-list">{invoicePager.items.map((invoice) => <div className="service-document-row" key={invoice.id}><span><strong>{invoice.invoice_number}</strong><small>{String(invoice.period_month).padStart(2, '0')}/{invoice.period_year} · {(invoice.total_cents / 100).toFixed(2)} {invoice.currency}</small></span><button className="button secondary" onClick={() => downloadPdf(`/billing/invoices/${invoice.id}/download`, token, `${invoice.invoice_number}.pdf`).catch((err) => setError(err.message))}>Télécharger</button></div>)}{!invoices.length && <p className="service-empty-note">Aucune facture générée pour cette entreprise.</p>}</div>
        <Pagination {...invoicePager} sizes={[5, 10, 25]} compact label="factures" />
      </article>
    </section>
  </div>
}

async function downloadPdf(path, token, filename) {
  const response = await fetch(`${API}${path}`, { headers: { Authorization: `Bearer ${token}` } })
  if (!response.ok) {
    let detail = `Erreur ${response.status}`
    try { detail = (await response.json()).detail || detail } catch { /* Non-JSON error response. */ }
    throw new Error(detail)
  }
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = filename
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
  window.setTimeout(() => URL.revokeObjectURL(url), 1000)
}

function BillingAdminPage({ token }) {
  const [companies, setCompanies] = useState([])
  const [selectedId, setSelectedId] = useState('')
  const [form, setForm] = useState(null)
  const [credits, setCredits] = useState({ ticket_credits: 0, month_credits: 0, note: '' })
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const selected = companies.find((company) => company.id === selectedId)
  const load = useCallback(async () => {
    try {
      const result = await request('/billing/companies', { token })
      setCompanies(result)
      if (!selectedId && result.length) setSelectedId(result[0].id)
    } catch (err) { setError(err.message) }
  }, [token, selectedId])
  useEffect(() => { load() }, [load])
  useEffect(() => {
    if (selected) setForm({
      billing_mode: selected.billing_mode,
      payment_mode: selected.payment_mode,
      payment_method: selected.payment_method,
      billing_currency: selected.billing_currency,
      monthly_fee_cents: selected.monthly_fee_cents,
      price_per_ticket_cents: selected.price_per_ticket_cents,
      tax_rate_basis_points: selected.tax_rate_basis_points,
      payment_terms_days: selected.payment_terms_days,
    })
  }, [selected?.id])
  async function saveBilling(event) {
    event.preventDefault()
    setBusy(true); setError(''); setNotice('')
    try {
      await request(`/billing/companies/${selectedId}`, { token, method: 'PUT', body: JSON.stringify(form) })
      setNotice('Paramètres de facturation enregistrés.')
      await load()
    } catch (err) { setError(err.message) } finally { setBusy(false) }
  }
  async function grant(event) {
    event.preventDefault()
    setBusy(true); setError(''); setNotice('')
    try {
      await request(`/billing/companies/${selectedId}/credits`, { token, method: 'POST', body: JSON.stringify(credits) })
      setNotice('Crédits attribués à l’entreprise.')
      setCredits({ ticket_credits: 0, month_credits: 0, note: '' })
      await load()
    } catch (err) { setError(err.message) } finally { setBusy(false) }
  }
  return <div className="team-page billing-admin-page">
    <div className="welcome-row"><div><span className="eyebrow">ADMINISTRATION FINANCIÈRE</span><h1>Facturation entreprises</h1><p>Configurez les tarifs, le paiement et les crédits prépayés.</p></div><button className="button secondary" onClick={load}><RefreshCw size={15} />Actualiser</button></div>
    {error && <div className="alert error page-alert">{error}</div>}{notice && <div className="alert success page-alert">{notice}</div>}
    <label className="service-company-select">Entreprise<select value={selectedId} onChange={(event) => setSelectedId(event.target.value)}>{companies.map((company) => <option key={company.id} value={company.id}>{company.name}</option>)}</select></label>
    {selected && form && <div className="billing-admin-grid">
      <form className="panel billing-settings-form" onSubmit={saveBilling}>
        <div className="panel-head"><div><h2>Paramètres de facturation</h2><p>{selected.name}</p></div><DollarSign size={18} className="muted-icon" /></div>
        <div className="billing-admin-fields">
          <label>Mode<select value={form.billing_mode} onChange={(e) => setForm({ ...form, billing_mode: e.target.value })}><option value="per_ticket">Par ticket</option><option value="monthly">Forfait mensuel</option></select></label>
          <label>Paiement<select value={form.payment_mode} onChange={(e) => setForm({ ...form, payment_mode: e.target.value })}><option value="postpaid">Postpayé</option><option value="prepaid">Prépayé (crédits requis)</option></select></label>
          <label>Devise<input maxLength="3" value={form.billing_currency} onChange={(e) => setForm({ ...form, billing_currency: e.target.value.toUpperCase() })} /></label>
          <label>Prix mensuel (centimes)<input type="number" min="0" value={form.monthly_fee_cents} onChange={(e) => setForm({ ...form, monthly_fee_cents: Number(e.target.value) })} /></label>
          <label>Prix par ticket (centimes)<input type="number" min="0" value={form.price_per_ticket_cents} onChange={(e) => setForm({ ...form, price_per_ticket_cents: Number(e.target.value) })} /></label>
          <label>Taxe (%)<input type="number" min="0" max="100" step="0.01" value={form.tax_rate_basis_points / 100} onChange={(e) => setForm({ ...form, tax_rate_basis_points: Math.round(Number(e.target.value) * 100) })} /></label>
          <label>Mode de paiement / référence<input maxLength="80" value={form.payment_method} onChange={(e) => setForm({ ...form, payment_method: e.target.value })} placeholder="Virement bancaire…" /></label>
          <label>Délai (jours)<input type="number" min="0" max="365" value={form.payment_terms_days} onChange={(e) => setForm({ ...form, payment_terms_days: Number(e.target.value) })} /></label>
        </div>
        <div className="billing-admin-actions"><button className="button primary" disabled={busy}>Enregistrer</button></div>
      </form>
      <form className="panel credit-grant-form" onSubmit={grant}>
        <div className="panel-head"><div><h2>Attribuer des crédits</h2><p>Les crédits sont ajoutés au solde de l’entreprise.</p></div><Sparkles size={18} className="muted-icon" /></div>
        <div className="billing-admin-fields"><label>Tickets<input type="number" min="0" value={credits.ticket_credits} onChange={(e) => setCredits({ ...credits, ticket_credits: Number(e.target.value) })} /></label><label>Mois d’accès<input type="number" min="0" value={credits.month_credits} onChange={(e) => setCredits({ ...credits, month_credits: Number(e.target.value) })} /></label><label className="billing-note-field">Motif<input value={credits.note} onChange={(e) => setCredits({ ...credits, note: e.target.value })} /></label></div>
        <div className="credit-balance"><span>Crédits tickets disponibles</span><strong>{selected.prepaid_ticket_credits}</strong><span>Crédits de mois disponibles</span><strong>{selected.prepaid_month_credits}</strong></div>
        <div className="billing-admin-actions"><button className="button primary" disabled={busy || (credits.ticket_credits === 0 && credits.month_credits === 0)}>Attribuer</button></div>
      </form>
    </div>}
    <p className="billing-disclaimer">Les PDF générés sont des relevés/factures de plateforme à compléter avec les mentions légales et fiscales applicables avant leur émission officielle.</p>
  </div>
}

function AITracePage({ token }) {
  const [catalog, setCatalog] = useState({ people: [], tickets: [] })
  const [traces, setTraces] = useState([])
  const [filters, setFilters] = useState({ ticket_id: '', customer_id: '', actor_id: '', provider: '', model: '', task: '', start: '', end: '' })
  const [offset, setOffset] = useState(0)
  const [pageSize, setPageSizeState] = useState(() => storedPageSize('ai-traces', 50))
  const [totalCount, setTotalCount] = useState(0)
  const [evaluations, setEvaluations] = useState({})
  const [loading, setLoading] = useState(true)
  const [busyId, setBusyId] = useState('')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const params = new URLSearchParams()
      Object.entries(filters).forEach(([key, value]) => {
        if (!value) return
        params.set(key, key === 'start' || key === 'end' ? new Date(value).toISOString() : value)
      })
      params.set('limit', String(pageSize))
      params.set('offset', String(offset))
      const [result, directory] = await Promise.all([
        request(`/admin/ai/traces?${params}`, { token }),
        request('/admin/ai/catalog', { token }),
      ])
      setTraces(result.items)
      setTotalCount(result.total_count)
      setCatalog(directory)
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }, [filters, offset, pageSize, token])

  useEffect(() => { load() }, [load])

  function setEvaluation(id, update) {
    setEvaluations((current) => ({ ...current, [id]: { ...current[id], ...update } }))
  }

  async function saveEvaluation(trace) {
    const value = evaluations[trace.id] || {}
    setBusyId(trace.id)
    setError('')
    setNotice('')
    try {
      const saved = await request(`/admin/ai/traces/${trace.id}`, {
        token,
        method: 'PATCH',
        body: JSON.stringify({
          score: value.score === undefined ? trace.evaluation_score : (value.score ? Number(value.score) : null),
          note: value.note === undefined ? trace.evaluation_note : value.note,
        }),
      })
      setTraces((items) => items.map((item) => item.id === trace.id ? { ...item, ...saved } : item))
      setNotice('Évaluation enregistrée.')
    } catch (err) {
      setError(err.message)
    } finally {
      setBusyId('')
    }
  }

  const setFilter = (key, value) => {
    setOffset(0)
    setFilters((current) => ({ ...current, [key]: value }))
  }
  const customers = catalog.people.filter((person) => person.role === 'customer')
  const operators = catalog.people.filter((person) => person.role !== 'customer')

  return <div className="ai-observability-page">
    <div className="welcome-row"><div><span className="eyebrow">ÉVALUATION DES MODÈLES</span><h1>Traces IA</h1><p>Consultez et notez les échanges enregistrés avec l’accord des clients. Conservation du contenu : 30 jours.</p></div><button className="button secondary" onClick={load} disabled={loading}><RefreshCw size={16} />Actualiser</button></div>
    {error && <div className="alert error page-alert">{error}</div>}
    {notice && <div className="alert success page-alert">{notice}</div>}
    <section className="panel ai-filter-panel">
      <div className="ai-filter-grid">
        <label>Client<select value={filters.customer_id} onChange={(event) => setFilter('customer_id', event.target.value)}><option value="">Tous</option>{customers.map((person) => <option key={person.id} value={person.id}>{person.name}</option>)}</select></label>
        <label>Utilisateur / opérateur<select value={filters.actor_id} onChange={(event) => setFilter('actor_id', event.target.value)}><option value="">Tous</option>{operators.map((person) => <option key={person.id} value={person.id}>{person.name} ({person.role})</option>)}</select></label>
        <label>Ticket<select value={filters.ticket_id} onChange={(event) => setFilter('ticket_id', event.target.value)}><option value="">Tous</option>{catalog.tickets.map((ticket) => <option key={ticket.id} value={ticket.id}>{ticket.reference} · {ticket.title}</option>)}</select></label>
        <label>Fournisseur<select value={filters.provider} onChange={(event) => setFilter('provider', event.target.value)}><option value="">Tous</option><option value="ollama">Ollama</option><option value="anthropic">Anthropic</option></select></label>
        <label>Modèle<input value={filters.model} onChange={(event) => setFilter('model', event.target.value)} placeholder="Nom du modèle" /></label>
        <label>Tâche<select value={filters.task} onChange={(event) => setFilter('task', event.target.value)}><option value="">Toutes</option><option value="ticket_analysis">Analyse initiale</option><option value="customer_diagnosis">Diagnostic client</option><option value="agent_assistance">Assistance agent</option><option value="handoff_summary">Résumé de transfert</option></select></label>
        <label>Depuis<input type="datetime-local" value={filters.start} onChange={(event) => setFilter('start', event.target.value)} /></label>
        <label>Jusqu’à<input type="datetime-local" value={filters.end} onChange={(event) => setFilter('end', event.target.value)} /></label>
      </div>
    </section>
    <div className="ai-trace-summary"><span>{loading ? 'Chargement…' : `${totalCount} trace${totalCount === 1 ? '' : 's'} trouvée${totalCount === 1 ? '' : 's'}`}</span><span>Prompts et réponses visibles uniquement avec consentement</span></div>
    {traces.length === 0 && !loading ? <div className="panel empty-state">Aucune trace ne correspond aux filtres. Les clients peuvent désactiver la conservation dans leurs consentements IA.</div> : <div className="ai-trace-list">
      {traces.map((trace) => {
        const evaluation = evaluations[trace.id] || {}
        return <article className="panel ai-trace-card" key={trace.id}>
          <div className="ai-trace-head"><div><strong>{trace.ticket_reference} · {trace.ticket_title}</strong><span>{trace.customer_name} · {trace.actor_name || 'Appel automatique'} · {date(trace.created_at)}</span></div><div className="ai-trace-tags"><span>{trace.provider}/{trace.model}</span><span>{trace.task}</span><span>{trace.input_tokens} in · {trace.output_tokens} out · {trace.duration_ms} ms</span></div></div>
          <div className="ai-trace-content"><section><h3>Prompt</h3><pre>{trace.prompt}</pre></section><section><h3>Réponse du modèle</h3><pre>{trace.response}</pre></section></div>
          <div className="ai-evaluation"><label>Note<select value={evaluation.score ?? trace.evaluation_score ?? ''} onChange={(event) => setEvaluation(trace.id, { score: event.target.value })}><option value="">Non évaluée</option><option value="1">1 · Insuffisante</option><option value="2">2</option><option value="3">3 · Moyenne</option><option value="4">4</option><option value="5">5 · Excellente</option></select></label><label>Commentaire<input value={evaluation.note ?? trace.evaluation_note ?? ''} maxLength={2000} onChange={(event) => setEvaluation(trace.id, { note: event.target.value })} placeholder="Qualité, exactitude, utilité…" /></label><button className="button secondary" disabled={busyId === trace.id} onClick={() => saveEvaluation(trace)}>{busyId === trace.id ? 'Enregistrement…' : 'Enregistrer l’évaluation'}</button></div>
        </article>
      })}
    </div>}
    <Pagination page={Math.floor(offset / pageSize) + 1} pageSize={pageSize} total={totalCount} sizes={[25, 50, 100, 200]} label="échanges" disabled={loading} setPage={(value) => setOffset((value - 1) * pageSize)} setPageSize={(size) => { setPageSizeState(size); rememberPageSize('ai-traces', size); setOffset(0) }} />
  </div>
}

function AICostPage({ token }) {
  const [catalog, setCatalog] = useState({ people: [], tickets: [] })
  const [prices, setPrices] = useState([])
  const [availableModels, setAvailableModels] = useState({})
  const [costs, setCosts] = useState({ items: [], total_estimated_usd: 0, unpriced_count: 0 })
  const [filters, setFilters] = useState({ ticket_id: '', customer_id: '', actor_id: '', provider: '', model: '', task: '', start: '', end: '' })
  const [offset, setOffset] = useState(0)
  const [pageSize, setPageSizeState] = useState(() => storedPageSize('ai-costs', 100))
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const params = new URLSearchParams()
      Object.entries(filters).forEach(([key, value]) => {
        if (!value) return
        params.set(key, key === 'start' || key === 'end' ? new Date(value).toISOString() : value)
      })
      params.set('limit', String(pageSize))
      params.set('offset', String(offset))
      const [costData, priceData, directory] = await Promise.all([
        request(`/admin/ai/costs?${params}`, { token }),
        request('/admin/ai/pricing', { token }),
        request('/admin/ai/catalog', { token }),
      ])
      setCosts(costData)
      setAvailableModels(priceData.available_models || {})
      setPrices(priceData.prices.map((item) => ({
        ...item,
        input_usd_per_million: item.input_usd_per_million ?? '',
        output_usd_per_million: item.output_usd_per_million ?? '',
      })))
      setCatalog(directory)
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }, [filters, offset, pageSize, token])

  useEffect(() => { load() }, [load])
  const setFilter = (key, value) => {
    setOffset(0)
    setFilters((current) => ({ ...current, [key]: value }))
  }
  const updatePrice = (index, key, value) => setPrices((current) => current.map((price, itemIndex) => itemIndex === index ? { ...price, [key]: value } : price))
  const updatePriceFields = (index, fields) => setPrices((current) => current.map((price, itemIndex) => itemIndex === index ? { ...price, ...fields } : price))

  function addPrice() {
    setPrices((current) => {
      const provider = (availableModels.ollama || []).some((name) => !current.some((price) => price.provider === 'ollama' && price.model === name)) ? 'ollama' : 'anthropic'
      const next = (availableModels[provider] || []).find((name) => !current.some((price) => price.provider === provider && price.model === name)) || ''
      return [...current, { provider, model: next, input_usd_per_million: provider === 'ollama' ? 0 : '', output_usd_per_million: provider === 'ollama' ? 0 : '', configured: false }]
    })
  }

  async function savePrices(event) {
    event.preventDefault()
    setSaving(true)
    setError('')
    setNotice('')
    try {
      const payload = prices.filter((price) => price.model.trim() && price.input_usd_per_million !== '' && price.output_usd_per_million !== '').map((price) => ({
        provider: price.provider,
        model: price.model.trim(),
        input_usd_per_million: Number(price.input_usd_per_million),
        output_usd_per_million: Number(price.output_usd_per_million),
      }))
      if (!payload.length) throw new Error('Renseignez au moins un tarif complet.')
      const saved = await request('/admin/ai/pricing', { token, method: 'PUT', body: JSON.stringify({ prices: payload }) })
      setPrices(saved.prices.map((item) => ({
        ...item,
        input_usd_per_million: item.input_usd_per_million ?? '',
        output_usd_per_million: item.output_usd_per_million ?? '',
      })))
      setNotice('Tarifs enregistrés. Les coûts affichés sont recalculés selon ces estimations.')
      await load()
    } catch (err) {
      setError(err.message)
    } finally {
      setSaving(false)
    }
  }

  const customers = catalog.people.filter((person) => person.role === 'customer')
  const operators = catalog.people.filter((person) => person.role !== 'customer')

  return <div className="ai-observability-page">
    <div className="welcome-row"><div><span className="eyebrow">SUIVI DES DÉPENSES</span><h1>Coûts IA estimés</h1><p>Calcul par échange à partir des tokens réellement retournés par le fournisseur et des tarifs configurés. Devise : USD.</p></div><button className="button secondary" onClick={load} disabled={loading}><RefreshCw size={16} />Actualiser</button></div>
    {error && <div className="alert error page-alert">{error}</div>}
    {notice && <div className="alert success page-alert">{notice}</div>}
    <section className="ai-cost-summary">
      <article className="stat-card"><div className="stat-icon green"><DollarSign size={18} /></div><span className="stat-label">Coût estimé filtré</span><strong className="stat-number">${Number(costs.total_estimated_usd || 0).toFixed(6)}</strong><span className="stat-hint">Tarifs fournisseur courants</span></article>
      <article className="stat-card"><div className="stat-icon blue"><Activity size={18} /></div><span className="stat-label">Appels filtrés</span><strong className="stat-number">{costs.total_count || 0}</strong><span className="stat-hint">Ollama local estimé à 0 $</span></article>
      <article className="stat-card"><div className="stat-icon red"><AlertCircle size={18} /></div><span className="stat-label">Sans tarif configuré</span><strong className="stat-number">{costs.unpriced_count}</strong><span className="stat-hint">Ces appels sont exclus du total estimé</span></article>
    </section>
    <section className="panel ai-filter-panel">
      <div className="panel-head"><div><h2>Filtrer les consommations</h2><p>Par ticket, client, utilisateur ayant lancé l’appel, fournisseur, modèle ou période.</p></div><ListFilter size={19} className="muted-icon" /></div>
      <div className="ai-filter-grid">
        <label>Client<select value={filters.customer_id} onChange={(event) => setFilter('customer_id', event.target.value)}><option value="">Tous</option>{customers.map((person) => <option key={person.id} value={person.id}>{person.name}</option>)}</select></label>
        <label>Admin / utilisateur<select value={filters.actor_id} onChange={(event) => setFilter('actor_id', event.target.value)}><option value="">Tous</option>{operators.map((person) => <option key={person.id} value={person.id}>{person.name} ({person.role})</option>)}</select></label>
        <label>Ticket<select value={filters.ticket_id} onChange={(event) => setFilter('ticket_id', event.target.value)}><option value="">Tous</option>{catalog.tickets.map((ticket) => <option key={ticket.id} value={ticket.id}>{ticket.reference} · {ticket.title}</option>)}</select></label>
        <label>Fournisseur<select value={filters.provider} onChange={(event) => setFilter('provider', event.target.value)}><option value="">Tous</option><option value="ollama">Ollama</option><option value="anthropic">Anthropic</option></select></label>
        <label>Modèle<input value={filters.model} onChange={(event) => setFilter('model', event.target.value)} placeholder="Nom du modèle" /></label>
        <label>Tâche<select value={filters.task} onChange={(event) => setFilter('task', event.target.value)}><option value="">Toutes</option><option value="ticket_analysis">Analyse initiale</option><option value="customer_diagnosis">Diagnostic client</option><option value="agent_assistance">Assistance agent</option><option value="handoff_summary">Résumé de transfert</option></select></label>
        <label>Depuis<input type="datetime-local" value={filters.start} onChange={(event) => setFilter('start', event.target.value)} /></label>
        <label>Jusqu’à<input type="datetime-local" value={filters.end} onChange={(event) => setFilter('end', event.target.value)} /></label>
      </div>
    </section>
    <section className="panel ai-cost-table-panel">
      <div className="panel-head"><div><h2>Détail des appels</h2><p>Les tarifs sont appliqués au nombre de tokens entrant et sortant.</p></div><DollarSign size={19} className="muted-icon" /></div>
      <div className="ai-cost-table-wrap"><table className="ai-cost-table"><thead><tr><th>Date</th><th>Ticket</th><th>Client</th><th>Utilisateur</th><th>Modèle</th><th>Tokens entrée/sortie</th><th>Coût estimé</th></tr></thead><tbody>
        {costs.items.map((item) => <tr key={item.id}><td>{date(item.created_at)}</td><td>{item.ticket_reference}</td><td>{item.customer_name}</td><td>{item.actor_name || 'Automatique'}</td><td>{item.provider}/{item.model}</td><td>{item.input_tokens} / {item.output_tokens}</td><td>{item.cost_usd === null ? 'Tarif manquant' : `$${Number(item.cost_usd).toFixed(6)}`}</td></tr>)}
        {!costs.items.length && <tr><td colSpan="7">{loading ? 'Chargement…' : 'Aucun appel pour ces filtres.'}</td></tr>}
      </tbody></table></div>
      <Pagination page={Math.floor(offset / pageSize) + 1} pageSize={pageSize} total={costs.total_count || 0} sizes={[50, 100, 200, 500]} label="appels" disabled={loading} setPage={(value) => setOffset((value - 1) * pageSize)} setPageSize={(size) => { setPageSizeState(size); rememberPageSize('ai-costs', size); setOffset(0) }} />
    </section>
    <section className="panel ai-pricing-panel">
      <div className="panel-head"><div><h2>Tarifs par modèle</h2><p>Prix en USD par million de tokens. Actualisez-les selon votre contrat fournisseur.</p></div><DollarSign size={19} className="muted-icon" /></div>
      <form onSubmit={savePrices}>
        <div className="ai-pricing-list">{prices.map((price, index) => <div className="ai-pricing-row" key={`${price.provider}-${price.model}-${index}`}>
          <select aria-label="Fournisseur" value={price.provider} onChange={(event) => updatePriceFields(index, { provider: event.target.value, model: '', custom: false })}><option value="anthropic">Anthropic</option><option value="ollama">Ollama</option></select>
          {(() => {
            const detected = availableModels[price.provider] || []
            const custom = Boolean(price.custom) || detected.length === 0 || (price.model !== '' && !detected.includes(price.model))
            return <div className="ai-pricing-model">
              {detected.length > 0 && <select aria-label="Modèle" value={custom ? '__custom__' : price.model} onChange={(event) => event.target.value === '__custom__' ? updatePriceFields(index, { custom: true, model: '' }) : updatePriceFields(index, { custom: false, model: event.target.value })}>
                <option value="" disabled>Choisir un modèle…</option>
                {detected.map((name) => <option key={name} value={name}>{name}</option>)}
                <option value="__custom__">Autre modèle…</option>
              </select>}
              {custom && <input aria-label="Identifiant du modèle" value={price.model} onChange={(event) => updatePrice(index, 'model', event.target.value)} placeholder="Identifiant exact du modèle" />}
            </div>
          })()}
          <label>Entrée / 1M<input type="number" min="0" step="0.000001" value={price.input_usd_per_million} onChange={(event) => updatePrice(index, 'input_usd_per_million', event.target.value)} placeholder="USD" /></label>
          <label>Sortie / 1M<input type="number" min="0" step="0.000001" value={price.output_usd_per_million} onChange={(event) => updatePrice(index, 'output_usd_per_million', event.target.value)} placeholder="USD" /></label>
          <span className={`price-state ${price.configured ? 'configured' : ''}`}>{price.configured ? 'Tarif utilisé' : 'À configurer'}</span>
        </div>)}</div>
        <div className="ai-pricing-actions"><button className="button secondary" type="button" onClick={addPrice}><Plus size={15} />Ajouter un modèle</button><button className="button primary" disabled={saving}>{saving ? 'Enregistrement…' : 'Enregistrer les tarifs'}</button></div>
      </form>
    </section>
  </div>
}

function App() {
  const [initialNavigation] = useState(readNavigationState)
  const [auth, setAuth] = useState(() => {
    try { return JSON.parse(localStorage.getItem('oxe-auth') || 'null') } catch { return null }
  })
  const [tickets, setTickets] = useState([])
  const [staff, setStaff] = useState([])
  const [companies, setCompanies] = useState([])
  const [accountRequests, setAccountRequests] = useState([])
  const [overview, setOverview] = useState(null)
  const [page, setPage] = useState(initialNavigation.page)
  const [selected, setSelected] = useState(null)
  const [selectedTicketId, setSelectedTicketId] = useState(initialNavigation.ticketId)
  const [filter, setFilter] = useState(initialNavigation.filter)
  const [search, setSearch] = useState(initialNavigation.search)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  const token = auth?.access_token
  const user = auth?.user
  const isStaff = user && user.role !== 'customer'
  const counters = useMemo(() => ({
    open: tickets.filter((ticket) => !['resolved', 'closed', 'rejected'].includes(ticket.status)).length,
    waiting: tickets.filter((ticket) => ticket.status === 'waiting_customer').length,
    resolved: tickets.filter((ticket) => ticket.status === 'resolved' || ticket.status === 'closed').length,
  }), [tickets])
  const loadTickets = useCallback(async () => {
    if (!token) return
    setLoading(true)
    setError('')
    try {
      const query = new URLSearchParams()
      if (filter !== 'all') query.set('status', filter)
      if (search.trim()) query.set('search', search.trim())
      query.set('limit', '1000')
      setTickets(await request(`/tickets${query.size ? `?${query}` : ''}`, { token }))
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }, [token, filter, search])

  useEffect(() => {
    if (auth) {
      localStorage.setItem('oxe-auth', JSON.stringify(auth))
      request('/auth/me', { token }).then((current) => {
        setAuth((value) => ({ ...value, user: current }))
      }).catch(() => {
        localStorage.removeItem('oxe-auth')
        setAuth(null)
      })
    } else localStorage.removeItem('oxe-auth')
  }, []) // Session is validated once when the app starts.

  useEffect(() => { loadTickets() }, [loadTickets])
  useEffect(() => {
    try {
      sessionStorage.setItem(NAVIGATION_STORAGE_KEY, JSON.stringify({
        page,
        ticketId: selectedTicketId,
        filter,
        search,
      }))
    } catch (err) {
      console.warn('Impossible de sauvegarder la navigation courante.', err)
    }
  }, [page, selectedTicketId, filter, search])

  useEffect(() => {
    if (!token || page !== 'ticket') return
    if (!selectedTicketId) {
      setPage('home')
      return
    }
    if (selected?.id === selectedTicketId) return
    let active = true
    request(`/tickets/${selectedTicketId}`, { token })
      .then((ticket) => {
        if (active) {
          setSelected(ticket)
          setTickets((current) => current.some((item) => item.id === ticket.id)
            ? current.map((item) => item.id === ticket.id ? ticket : item)
            : [ticket, ...current])
        }
      })
      .catch((err) => {
        if (active) {
          setError(err.message)
          setSelectedTicketId(null)
          setPage('home')
        }
      })
    return () => { active = false }
  }, [page, selectedTicketId, selected?.id, token])

  useEffect(() => {
    if (user?.role === 'customer' && ['supervision', 'ai-routing', 'ai-traces', 'ai-costs', 'rag', 'kb', 'companies', 'access-requests', 'team', 'billing'].includes(page)) {
      setPage('home')
    }
    if (user && user.role !== 'customer' && page === 'ai-consent') setPage('home')
    if (user && user.role !== 'admin' && page === 'ai-diagnostics') setPage('home')
  }, [page, user?.role])

  useEffect(() => {
    if (user && user.role !== 'customer') request('/users', { token }).then(setStaff).catch((err) => setError(err.message))
  }, [token, user?.role])

  useEffect(() => {
    if (user && user.role !== 'customer') {
      request('/companies', { token }).then(setCompanies).catch((err) => setError(err.message))
      request('/users/requests', { token }).then(setAccountRequests).catch((err) => setError(err.message))
    } else {
      setCompanies([])
      setAccountRequests([])
    }
  }, [token, user?.role])

  useEffect(() => {
    if (user?.role === 'admin') {
      request('/system/overview', { token }).then(setOverview).catch((err) => setError(err.message))
    } else {
      setOverview(null)
    }
  }, [token, user?.role])

  function handleAuth(value) {
    localStorage.setItem('oxe-auth', JSON.stringify(value))
    setAuth(value)
  }
  function logout() {
    localStorage.removeItem('oxe-auth')
    sessionStorage.removeItem(NAVIGATION_STORAGE_KEY)
    setAuth(null)
    setTickets([])
    setSelected(null)
    setSelectedTicketId(null)
    setFilter('all')
    setSearch('')
    setPage('home')
  }
  function openTicket(id) {
    setSelected(null)
    setSelectedTicketId(id)
    setPage('ticket')
  }
  async function refreshTicket(id = selected?.id) {
    if (!id) return
    const ticket = await request(`/tickets/${id}`, { token })
    setSelected(ticket)
    setTickets((current) => current.map((item) => item.id === ticket.id ? ticket : item))
    return ticket
  }
  async function createTicket(payload) {
    const ticket = await request('/tickets', { token, method: 'POST', body: JSON.stringify(payload) })
    setTickets((current) => [ticket, ...current])
    setSelected(ticket)
    setSelectedTicketId(ticket.id)
    setPage('ticket')
  }

  const [navCollapsed, setNavCollapsed] = useState(() => {
    try { return localStorage.getItem('oxe-nav-collapsed') === '1' } catch { return false }
  })
  const toggleNav = () => setNavCollapsed((current) => {
    try { localStorage.setItem('oxe-nav-collapsed', current ? '0' : '1') } catch { /* stockage indisponible */ }
    return !current
  })

  if (!auth || !user) return <AuthScreen onAuth={handleAuth} />

  const isAdmin = user.role === 'admin'
  const canUseConsole = isStaff || user.is_individual
  const navGroups = [
    { label: 'Support', items: [
      { page: 'home', label: isStaff ? 'File de support' : 'Mes demandes', icon: Activity, count: tickets.length },
      canUseConsole && { page: 'help', label: 'Console d’aide', icon: Sparkles },
    ] },
    { label: 'Entreprise', items: [
      isStaff && { page: 'companies', label: 'Entreprises', icon: Users },
      isStaff && { page: 'team', label: 'Utilisateurs', icon: Users },
      isStaff && { page: 'access-requests', label: 'Demandes d’accès', icon: CheckCheck, count: accountRequests.length },
      { page: 'company-docs', label: 'Documents entreprise', icon: FileText },
    ] },
    { label: 'Service & facturation', items: [
      { page: 'service', label: 'SLA, rapports & factures', icon: Clock3 },
      isAdmin && { page: 'billing', label: 'Facturation', icon: DollarSign },
    ] },
    { label: 'Connaissances', items: [
      isAdmin && { page: 'rag', label: 'Corpus RAG', icon: Database },
      isAdmin && { page: 'kb', label: 'KB constructeur', icon: BookOpen },
    ] },
    { label: 'Intelligence artificielle', items: [
      isAdmin && { page: 'ai-routing', label: 'Configuration IA', icon: BrainCircuit },
      isAdmin && { page: 'ai-traces', label: 'Évaluation LLM', icon: ClipboardList },
      isAdmin && { page: 'ai-costs', label: 'Coûts IA', icon: DollarSign },
      isAdmin && { page: 'ai-diagnostics', label: 'Diagnostic IA', icon: Activity },
    ] },
    { label: 'Système', items: [
      isAdmin && { page: 'supervision', label: 'Supervision', icon: ShieldCheck },
    ] },
    { label: 'Mon compte', items: [
      user.role === 'customer' && { page: 'ai-consent', label: 'Confidentialité IA', icon: ShieldCheck },
      { page: 'profile', label: 'Mon profil', icon: UserRound },
    ] },
  ].map((group) => ({ ...group, items: group.items.filter(Boolean) })).filter((group) => group.items.length > 0)

  return (
    <div className={`app-shell ${navCollapsed ? 'nav-collapsed' : ''}`}>
      <aside className={`sidebar ${navCollapsed ? 'collapsed' : ''}`}>
        <div className="brand"><span className="brand-icon"><Headset size={20} /></span><span>OXE <b>SUPPORT</b></span></div>
        <button type="button" className="sidebar-toggle" onClick={toggleNav} aria-label={navCollapsed ? 'Déplier le menu' : 'Réduire le menu'} title={navCollapsed ? 'Déplier le menu' : 'Réduire le menu'}>{navCollapsed ? <ChevronsRight size={16} /> : <ChevronsLeft size={16} />}{!navCollapsed && <span>Réduire</span>}</button>
        <nav aria-label="Navigation principale">
          {navGroups.map((group) => <div className="nav-group" key={group.label} role="group" aria-label={group.label}>
            <div className="nav-group-label">{group.label}</div>
            {group.items.map((item) => <button key={item.page} className={`nav-item ${page === item.page ? 'active' : ''}`} onClick={() => setPage(item.page)} title={navCollapsed ? item.label : undefined} aria-label={item.label}>
              <item.icon size={18} /><span className="nav-label">{item.label}</span>{item.count != null && <span className="nav-count">{item.count}</span>}
            </button>)}
          </div>)}
        </nav>
        <div className="sidebar-bottom">
          {user.role === 'customer' && <div className="help-card"><div className="help-icon"><CircleHelp size={18} /></div><strong>Besoin d'aide ?</strong><p>Notre équipe est là pour vous accompagner.</p><button onClick={() => setPage('new')}>Contacter le support <ArrowUpRight size={14} /></button></div>}
          <div className="profile">
            <Avatar name={user.full_name} />
            <div className="profile-meta"><strong>{user.full_name}</strong><span>{user.role === 'admin' ? 'Administrateur' : user.role === 'agent' ? 'Agent support' : 'Espace client'}</span></div>
            <button className="icon-button" title="Déconnexion" onClick={logout}><LogOut size={17} /></button>
          </div>
        </div>
      </aside>
      <main className="main-area">
        <header className="topbar">
          <div className="breadcrumbs"><span>Support</span><span className="crumb-slash">/</span><b>{page === 'profile' ? 'Mon profil' : page === 'ticket' ? selected?.reference : page === 'new' ? 'Nouveau ticket' : page === 'team' ? 'Utilisateurs' : page === 'supervision' ? 'Supervision' : page === 'ai-routing' ? 'Configuration IA' : page === 'ai-traces' ? 'Évaluation LLM' : page === 'ai-costs' ? 'Coûts IA' : page === 'ai-diagnostics' ? 'Diagnostic IA' : page === 'ai-consent' ? 'Confidentialité IA' : page === 'rag' ? 'Corpus RAG' : page === 'kb' ? 'KB constructeur' : page === 'company-docs' ? 'Documents entreprise' : page === 'help' ? 'Console d’aide' : page === 'companies' ? 'Entreprises' : page === 'access-requests' ? 'Demandes d’accès' : page === 'service' ? 'SLA, rapports & factures' : page === 'billing' ? 'Facturation' : isStaff ? 'File de support' : 'Mes demandes'}</b></div>
          <div className="topbar-right"><span className="online-pill"><span className="live-dot" /> Support disponible</span><Avatar name={user.full_name} size="small" /></div>
        </header>
        <div className="content">
          {error && <div className="alert error page-alert"><AlertCircle size={17} />{error}<button onClick={() => setError('')}>Fermer</button></div>}
          {page === 'home' && <Dashboard
            user={user} tickets={tickets} loading={loading} filter={filter} setFilter={setFilter}
            search={search} setSearch={setSearch} counters={counters} onNew={() => setPage('new')}
            onOpen={openTicket} onRefresh={loadTickets}
          />}
          {page === 'new' && <NewTicket token={token} onCancel={() => setPage('home')} onCreate={createTicket} />}
          {page === 'ticket' && selected && <TicketDetail
            ticket={selected} user={user} staff={staff} token={token}
            onBack={() => { setPage('home'); loadTickets() }}
            onChange={() => refreshTicket()}
          />}
          {page === 'ticket' && !selected && <div className="empty-state">Chargement de la demande…</div>}
          {page === 'supervision' && user.role === 'admin' && <SupervisionPage overview={overview} />}
          {page === 'ai-routing' && user.role === 'admin' && <AIRoutingPage token={token} />}
          {page === 'ai-traces' && user.role === 'admin' && <AITracePage token={token} />}
          {page === 'ai-costs' && user.role === 'admin' && <AICostPage token={token} />}
          {page === 'ai-diagnostics' && user.role === 'admin' && <AIDiagnosticsPage token={token} />}
          {page === 'service' && <ServicePortalPage token={token} user={user} companies={companies} />}
          {page === 'billing' && user.role === 'admin' && <BillingAdminPage token={token} />}
          {page === 'help' && (isStaff || user.is_individual) && <HelpConsolePage token={token} user={user} tickets={tickets} />}
          {page === 'company-docs' && <CompanyDocsPage token={token} user={user} companies={companies} />}
          {page === 'ai-consent' && user.role === 'customer' && <AIConsentPage
            user={user} token={token}
            onSaved={(updated) => setAuth((current) => {
              const next = { ...current, user: updated }
              localStorage.setItem('oxe-auth', JSON.stringify(next))
              return next
            })}
          />}
          {page === 'profile' && <ProfilePage
            user={user} companies={companies} token={token}
            onAuthChanged={(updated) => {
              setAuth(updated)
              localStorage.setItem('oxe-auth', JSON.stringify(updated))
            }}
          />}
          {page === 'rag' && user.role === 'admin' && <RagAdminPage token={token} />}
          {page === 'kb' && user.role === 'admin' && <KBAdminPage token={token} onOpenTicket={openTicket} />}
          {page === 'companies' && isStaff && <CompaniesPage
            companies={companies} staff={staff} user={user} token={token}
            onCreated={(company) => setCompanies((current) => [...current, company])}
            onUpdated={(company) => setCompanies((current) => current.map((item) => item.id === company.id ? company : item))}
          />}
          {page === 'access-requests' && isStaff && <AccountRequestsPage
            requests={accountRequests} companies={companies} user={user} token={token}
            onDecision={(userId) => setAccountRequests((current) => current.filter((item) => item.id !== userId))}
            onUpdated={(updated) => setAccountRequests((current) => current.map((item) => item.id === updated.id ? updated : item))}
          />}
          {page === 'team' && isStaff && <TeamPage
            staff={staff} companies={companies} token={token} user={user}
            onCreated={(member) => setStaff((value) => [...value, member])}
            onUpdated={(updated) => {
              setStaff((value) => value.map((member) => member.id === updated.id ? updated : member))
              setAuth((current) => {
                if (current?.user?.id !== updated.id) return current
                const next = { ...current, user: updated }
                localStorage.setItem('oxe-auth', JSON.stringify(next))
                return next
              })
            }}
          />}
        </div>
      </main>
    </div>
  )
}

function Dashboard({ user, tickets, loading, filter, setFilter, search, setSearch, counters, onNew, onOpen, onRefresh }) {
  const isStaff = user.role !== 'customer'
  const pager = usePagination(tickets, { storageKey: 'tickets', initialSize: 25, resetKey: `${filter}|${search}` })
  return (
    <>
      <section className="welcome-row">
        <div><span className="eyebrow">{isStaff ? 'CENTRE DE SUPPORT' : 'VOTRE ESPACE SUPPORT'}</span><h1>{isStaff ? 'Bonjour, ' : 'Bonjour, '}{user.full_name.split(' ')[0]}<span className="wave"> ✦</span></h1><p>{isStaff ? 'Voici les incidents à traiter aujourd’hui.' : 'Retrouvez vos incidents et échangez avec nos experts.'}</p></div>
        {!isStaff && <button className="button primary" onClick={onNew}><Plus size={18} />Nouveau ticket</button>}
      </section>
      <section className="stat-grid">
        <StatCard icon={<LifeBuoy size={18} />} label={isStaff ? 'Incidents actifs' : 'Demandes ouvertes'} value={counters.open} hint={isStaff ? 'Dans la file de traitement' : 'En cours de traitement'} tone="blue" />
        <StatCard icon={<Clock3 size={18} />} label="En attente client" value={counters.waiting} hint="Réponse du client attendue" tone="red" />
        <StatCard icon={<CheckCheck size={18} />} label="Résolus" value={counters.resolved} hint="Résolus ou clôturés" tone="green" />
      </section>
      <section className="panel ticket-panel">
        <div className="panel-head"><div><h2>{isStaff ? 'Incidents récents' : 'Vos demandes'}</h2><p>{isStaff ? 'Priorisez et prenez en charge les demandes.' : 'Historique et état d’avancement de vos demandes.'}</p></div><button className="icon-button refresh-button" title="Actualiser" onClick={onRefresh}><Activity size={17} /></button></div>
        <div className="table-tools">
          <div className="search-box"><Search size={16} /><input placeholder="Rechercher un incident…" value={search} onChange={(e) => setSearch(e.target.value)} /></div>
          <div className="filter-box"><ListFilter size={15} /><select value={filter} onChange={(e) => setFilter(e.target.value)}><option value="all">Tous les statuts</option>{Object.entries(statuses).map(([key, [label]]) => <option key={key} value={key}>{label}</option>)}</select><ChevronDown size={14} /></div>
        </div>
        <div className="ticket-list">
          {loading ? <div className="empty-state">Chargement des incidents…</div> : tickets.length === 0 ? <div className="empty-state"><span className="empty-icon"><MessageCircle size={22} /></span><strong>Aucun incident pour le moment</strong><span>{isStaff ? 'Les nouvelles demandes apparaîtront ici.' : 'Créez une demande pour être accompagné par notre équipe.'}</span>{!isStaff && <button className="button secondary" onClick={onNew}><Plus size={16} />Créer une demande</button>}</div> : pager.items.map((ticket) => (
            <button className="ticket-row" key={ticket.id} onClick={() => onOpen(ticket.id)}>
              <span className="ticket-type-icon"><Headset size={17} /></span>
              <span className="ticket-main"><strong>{ticket.title}</strong><span>{ticket.reference} <i>·</i> {ticket.product}{ticket.version ? ` ${ticket.version}` : ''} <i>·</i> {date(ticket.updated_at)}</span></span>
              <span className="ticket-priority"><Badge value={ticket.priority} map={priorities} /></span>
              <Badge value={ticket.status} />
              <ArrowUpRight className="row-arrow" size={17} />
            </button>
          ))}
        </div>
        <Pagination {...pager} label="incidents" />
        <div className="panel-foot"><span>{tickets.length} incident{tickets.length > 1 ? 's' : ''}</span><span><span className="live-dot" /> Actualisé à l’instant</span></div>
      </section>
      <div className="dashboard-note"><Sparkles size={16} /><span><strong>Assistance augmentée par l’IA</strong> — chaque incident est analysé pour proposer des premières pistes de résolution.</span></div>
    </>
  )
}

function StatCard({ icon, label, value, hint, tone }) {
  return <div className="stat-card"><div className={`stat-icon ${tone}`}>{icon}</div><span className="stat-label">{label}</span><strong className="stat-number">{value}</strong><span className="stat-hint">{hint}</span></div>
}

function useAIOptions(token) {
  const [options, setOptions] = useState(null)
  useEffect(() => {
    let active = true
    request('/ai/options', { token })
      .then((data) => { if (active) setOptions(data) })
      .catch(() => { if (active) setOptions({ enabled: false, models: [], default: null }) })
    return () => { active = false }
  }, [token])
  return options
}

function AIModelPicker({ options, value, onChange, disabled = false, label = 'Modèle IA' }) {
  if (!options?.enabled || options.models.length === 0) return null
  const current = value?.model ? value : options.default
  const selected = options.models.find((item) => item.provider === current?.provider && item.model === current?.model) || options.models[0]
  const think = current?.provider === selected.provider && current?.model === selected.model ? Boolean(current.think) : selected.think
  function pick(key) {
    const next = options.models.find((item) => `${item.provider}::${item.model}` === key)
    onChange({ provider: next.provider, model: next.model, think: next.supports_think ? next.think : false })
  }
  return <div className="ai-model-picker">
    <label>{label}<select value={`${selected.provider}::${selected.model}`} disabled={disabled} onChange={(event) => pick(event.target.value)}>{options.models.map((item) => <option key={`${item.provider}::${item.model}`} value={`${item.provider}::${item.model}`}>{item.model}{item.provider === 'anthropic' ? ' (Claude)' : ''}</option>)}</select></label>
    {selected.supports_think && <label className="ai-option-check" title="Le modèle réfléchit avant de répondre : réponse plus approfondie mais plus lente."><input type="checkbox" checked={think} disabled={disabled} onChange={(event) => onChange({ provider: selected.provider, model: selected.model, think: event.target.checked })} />Activer le raisonnement</label>}
  </div>
}

function NewTicket({ token, onCancel, onCreate }) {
  const [form, setForm] = useState({ title: '', product: 'OXE', version: '', description: '', ticket_type: 'incident', priority: 'medium' })
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const aiOptions = useAIOptions(token)
  const [aiChoice, setAiChoice] = useState(null)
  async function submit(event) {
    event.preventDefault()
    setBusy(true)
    setError('')
    try { await onCreate({ ...form, ...(aiChoice ? { ai_model: aiChoice } : {}) }) } catch (err) { setError(err.message) } finally { setBusy(false) }
  }
  return <div className="form-page">
    <button className="back-link" onClick={onCancel}><ArrowLeft size={16} />Retour aux demandes</button>
    <div className="form-heading"><span className="eyebrow">DIAGNOSTIC GUIDÉ</span><h1>Nouveau ticket</h1><p>Décrivez votre incident, votre problème ou votre demande. Un assistant IA vous accompagnera d’abord dans les vérifications initiales. Vous pourrez transmettre l’échange à un agent après avoir répondu à sa première demande.</p></div>
    <form className="panel incident-form" onSubmit={submit}>
      {error && <div className="alert error"><AlertCircle size={17} />{error}</div>}
      <div className="form-section-title"><span className="step-number">01</span><div><strong>Informations sur le ticket</strong><small>Quelques informations pour orienter le traitement.</small></div></div>
      <label>Objet du ticket<input required minLength={5} maxLength={180} placeholder="Ex. Les appels entrants n'aboutissent plus" value={form.title} onChange={(e) => setForm({ ...form, title: e.target.value })} /></label>
      <div className="form-grid"><label>Type de ticket<select value={form.ticket_type} onChange={(e) => setForm({ ...form, ticket_type: e.target.value })}>{Object.entries(ticketTypes).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></label><label>Priorité<select value={form.priority} onChange={(e) => setForm({ ...form, priority: e.target.value })}><option value="critical">P1 · Critique</option><option value="high">P2 · Haute</option><option value="medium">P3 · Normale</option><option value="low">P4 · Basse</option></select></label></div>
      <div className="form-grid"><label>Produit<select value={form.product} onChange={(e) => setForm({ ...form, product: e.target.value })}><option>OXE</option><option>OXO Connect</option><option>OmniPCX Enterprise</option><option>Autre solution Alcatel-Lucent</option></select></label><label>Version logicielle <span className="optional">— facultatif</span><input placeholder="Ex. R12.4" value={form.version} onChange={(e) => setForm({ ...form, version: e.target.value })} /></label></div>
      <label>Description détaillée<textarea required minLength={20} maxLength={10000} rows={7} placeholder="Décrivez le comportement observé, les messages d'erreur, le périmètre impacté et les actions déjà réalisées…" value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} /><span className="field-hint">Évitez d’inclure des mots de passe ou des données personnelles sensibles.</span></label>
      <div className="ai-explainer"><Sparkles size={18} /><div><strong>Session de diagnostic IA</strong><span>La session s’ouvre immédiatement. Ajoutez des journaux ou captures pendant l’échange; si l’IA ne résout pas le problème, transférez le dossier complet au support.</span></div></div>
      <AIModelPicker options={aiOptions} value={aiChoice} onChange={setAiChoice} label="Modèle IA pour le diagnostic" />
      <div className="form-actions"><button className="button secondary" type="button" onClick={onCancel}>Annuler</button><button className="button primary" disabled={busy}><Send size={16} />{busy ? 'Ouverture…' : 'Démarrer le diagnostic'}</button></div>
    </form>
  </div>
}

function TicketDetail({ ticket, user, staff, token, onBack, onChange }) {
  const [message, setMessage] = useState('')
  const [files, setFiles] = useState([])
  const [internal, setInternal] = useState(false)
  const [busy, setBusy] = useState(false)
  const [aiQuestion, setAiQuestion] = useState('')
  const [aiBusy, setAiBusy] = useState(false)
  const aiOptions = useAIOptions(token)
  const [agentChoice, setAgentChoice] = useState(null)
  const [error, setError] = useState('')
  const [selectedStatus, setSelectedStatus] = useState(ticket.status)
  const [statusNote, setStatusNote] = useState('')
  const [complianceFinding, setComplianceFinding] = useState('')
  const [complianceNote, setComplianceNote] = useState('')
  const [waitingForAiReply, setWaitingForAiReply] = useState(false)
  const isStaff = user.role !== 'customer'
  const isAiThinking = !isStaff
    && ticket.status === 'ai_triage'
    && (ticket.ai_status === 'pending' || waitingForAiReply)
  const isThreadAiThinking = isAiThinking || (isStaff && aiBusy)
  const firstAiResponseIndex = ticket.messages.findIndex(
    (item) => item.author_type === 'ai' && item.visibility === 'public',
  )
  const hasRepliedToFirstAi = firstAiResponseIndex >= 0 && ticket.messages
    .slice(firstAiResponseIndex + 1)
    .some((item) => item.author_id === user.id && item.visibility === 'public')

  useEffect(() => {
    if (ticket.ai_status !== 'pending') setWaitingForAiReply(false)
    if (!isAiThinking) return undefined
    let active = true
    let timer
    const refreshWhileThinking = async () => {
      try {
        await onChange()
      } catch (err) {
        if (active) setError(err.message)
      }
      if (active) timer = window.setTimeout(refreshWhileThinking, 2000)
    }
    timer = window.setTimeout(refreshWhileThinking, 2000)
    return () => {
      active = false
      window.clearTimeout(timer)
    }
  }, [isAiThinking, isStaff, ticket.id, ticket.status, ticket.ai_status])

  async function perform(path, method, body) {
    setBusy(true)
    setError('')
    try {
      await request(path, { token, method, body: JSON.stringify(body) })
      await onChange()
      return true
    } catch (err) { setError(err.message); return false } finally { setBusy(false) }
  }
  async function sendMessage(event) {
    event.preventDefault()
    if (!message.trim() && files.length === 0) return
    if (!isStaff) {
      setBusy(true)
      setError('')
      if (ticket.status === 'ai_triage') setWaitingForAiReply(true)
      try {
        const form = new FormData()
        form.set('body', message)
        files.forEach((file) => form.append('files', file))
        await request(`/tickets/${ticket.id}/conversation`, { token, method: 'POST', body: form })
        const updated = await onChange()
        setWaitingForAiReply(updated?.status === 'ai_triage' && updated?.ai_status === 'pending')
      } catch (err) {
        setWaitingForAiReply(false)
        setError(err.message)
        return
      } finally {
        setBusy(false)
      }
    } else {
      if (message.trim()) {
        const sent = await perform(`/tickets/${ticket.id}/messages`, 'POST', { body: message, visibility: internal ? 'internal' : 'public' })
        if (!sent) return
        setMessage('')
        setInternal(false)
      }
      if (files.length) {
        setBusy(true)
        try {
          const form = new FormData()
          files.forEach((file) => form.append('files', file))
          await request(`/tickets/${ticket.id}/attachments`, { token, method: 'POST', body: form })
          await onChange()
          setFiles([])
        } catch (err) {
          setError(err.message)
          return
        } finally {
          setBusy(false)
        }
      }
    }
    setMessage('')
    if (!isStaff) setFiles([])
  }
  async function updateStatus(event) {
    const nextStatus = event.target.value
    setSelectedStatus(nextStatus)
    setStatusNote('')
    if (nextStatus === 'resolved' || nextStatus === 'rejected') return
    await perform(`/tickets/${ticket.id}/status`, 'PATCH', { status: nextStatus })
  }
  async function submitStatusNote(event) {
    event.preventDefault()
    const payload = selectedStatus === 'resolved'
      ? { status: selectedStatus, resolution_note: statusNote }
      : { status: selectedStatus, rejection_reason: statusNote }
    if (await perform(`/tickets/${ticket.id}/status`, 'PATCH', payload)) {
      setStatusNote('')
    }
  }
  async function closeTicket() {
    await perform(`/tickets/${ticket.id}/close`, 'POST', {})
  }
  async function assign(event) {
    await perform(`/tickets/${ticket.id}/assignment`, 'PATCH', { agent_id: event.target.value || null })
  }
  async function handoff() {
    await perform(`/tickets/${ticket.id}/handoff`, 'POST', { reason: 'Le diagnostic initial par IA n’a pas résolu le problème.' })
  }
  async function recordCompliance(event) {
    event.preventDefault()
    if (await perform(`/tickets/${ticket.id}/sla/compliance`, 'POST', {
      finding: complianceFinding,
      note: complianceNote,
    })) {
      setComplianceFinding('')
      setComplianceNote('')
    }
  }
  async function markWorkaround() {
    await perform(`/tickets/${ticket.id}/sla/workaround`, 'POST', {})
  }
  async function selectCustomerModel(choice) {
    setError('')
    try {
      await request(`/tickets/${ticket.id}/ai-preferences`, { token, method: 'PUT', body: JSON.stringify(choice) })
      await onChange()
    } catch (err) {
      setError(err.message)
    }
  }
  async function requestAiHelp(event) {
    event.preventDefault()
    if (!aiQuestion.trim()) return
    await runAiAssist({ question: aiQuestion }, () => setAiQuestion(''))
  }
  async function requestKbSearch() {
    setError('')
    try {
      const result = await request(`/tickets/${ticket.id}/kb-enrich`, { token, method: 'POST' })
      setError(result.status === 'queued' ? '' : 'Une recherche KB est déjà en attente pour ce ticket.')
      window.setTimeout(() => { onChange().catch(() => {}) }, 20000)
    } catch (err) {
      setError(err.message)
    }
  }
  async function requestSummary() {
    await runAiAssist({ kind: 'summary' })
  }
  async function runAiAssist(payload, onSent) {
    const existingAiMessages = new Set(ticket.messages.filter((item) => item.author_type === 'ai' && item.visibility === 'internal').map((item) => item.id))
    setAiBusy(true)
    setError('')
    try {
      await request(`/tickets/${ticket.id}/ai-assist`, {
        token,
        method: 'POST',
        body: JSON.stringify({ ...payload, ...(agentChoice ? { ai_model: agentChoice } : {}) }),
      })
      if (onSent) onSent()
      for (let attempt = 0; attempt < 60; attempt += 1) {
        await new Promise((resolve) => setTimeout(resolve, 2000))
        const updated = await onChange()
        if (updated?.messages?.some((item) => item.author_type === 'ai' && item.visibility === 'internal' && !existingAiMessages.has(item.id))) break
      }
    } catch (err) {
      setError(err.message)
    } finally {
      setAiBusy(false)
    }
  }
  async function downloadAttachment(attachment) {
    try {
      const response = await fetch(`${API}${attachment.download_url}`, {
        headers: token ? { Authorization: `Bearer ${token}` } : {},
      })
      if (!response.ok) throw new Error(`Téléchargement impossible (${response.status}).`)
      const url = URL.createObjectURL(await response.blob())
      const link = document.createElement('a')
      link.href = url
      link.download = attachment.file_name
      link.click()
      URL.revokeObjectURL(url)
    } catch (err) {
      setError(err.message)
    }
  }

  useEffect(() => {
    if (isStaff || ticket.status !== 'ai_triage' || ticket.ai_status !== 'pending') return undefined
    const timer = window.setTimeout(() => {
      onChange().catch((err) => setError(err.message))
    }, 2000)
    return () => window.clearTimeout(timer)
  }, [isStaff, ticket.id, ticket.status, ticket.ai_status, ticket.updated_at, onChange])

  useEffect(() => {
    setSelectedStatus(ticket.status)
    setStatusNote('')
  }, [ticket.status])

  const nextStaffStatuses = {
    new: ['in_progress', 'waiting_support', 'waiting_customer', 'resolved', 'rejected'],
    in_progress: ['new', 'waiting_support', 'waiting_customer', 'resolved', 'rejected'],
    waiting_support: ['new', 'in_progress', 'waiting_customer', 'resolved', 'rejected'],
    waiting_customer: ['new', 'in_progress', 'waiting_support', 'resolved', 'rejected'],
    escalated: ['in_progress', 'waiting_support', 'waiting_customer', 'resolved', 'rejected'],
    resolved: ticket.resolution_note ? [] : ['resolved'],
  }[ticket.status] || []
  const statusOptions = [...new Set([ticket.status, ...nextStaffStatuses, selectedStatus])]
  // Assignables : agents affectés à l'entreprise du client; les administrateurs ne sont proposés qu'à un administrateur.
  const ticketCompanyId = staff.find((member) => member.id === ticket.customer_id)?.company_id
  const assignable = staff.filter((member) => member.is_active && (
    (member.role === 'agent' && (!ticketCompanyId || (member.company_ids || []).includes(ticketCompanyId)))
    || (member.role === 'admin' && (user.role === 'admin' || member.id === ticket.assigned_agent_id))
  ))
  if (ticket.assigned_agent_id && !assignable.some((member) => member.id === ticket.assigned_agent_id)) {
    assignable.push({ id: ticket.assigned_agent_id, full_name: ticket.assigned_agent_name || 'Agent', role: 'agent' })
  }

  return <div className="detail-page">
    <button className="back-link" onClick={onBack}><ArrowLeft size={16} />Retour à la liste</button>
    {error && <div className="alert error"><AlertCircle size={17} />{error}<button onClick={() => setError('')}>Fermer</button></div>}
    <div className="detail-title-row"><div><div className="detail-ref"><span>{ticket.reference}</span><span className="ref-line" />{date(ticket.created_at)}</div><h1>{ticket.title}</h1><div className="detail-tags"><Badge value={ticket.status} /><Badge value={ticket.priority} map={priorities} /><span className="plain-tag">{ticketTypes[ticket.ticket_type] || ticket.ticket_type} · {ticket.product}{ticket.version ? ` · ${ticket.version}` : ''}</span></div></div>{isStaff && nextStaffStatuses.length > 0 && <div className="status-control"><label>Statut<select value={selectedStatus} onChange={updateStatus}>{statusOptions.map((key) => <option key={key} value={key}>{statuses[key][0]}</option>)}</select></label>{(selectedStatus === 'resolved' || selectedStatus === 'rejected') && <form className="status-note-form" onSubmit={submitStatusNote}><label>{selectedStatus === 'resolved' ? 'Note de résolution (visible par le client)' : 'Motif du rejet (visible par le client)'}<textarea required minLength={10} maxLength={5000} rows={3} value={statusNote} onChange={(event) => setStatusNote(event.target.value)} placeholder={selectedStatus === 'resolved' ? 'Décrivez la cause et la solution appliquée…' : 'Expliquez pourquoi la demande ne peut pas être prise en charge…'} /></label><button className="button primary" disabled={busy || statusNote.trim().length < 10}>{selectedStatus === 'resolved' ? 'Proposer la résolution' : 'Rejeter la demande'}</button><button className="button secondary" type="button" onClick={() => { setSelectedStatus(ticket.status); setStatusNote('') }}>Annuler</button></form>}</div>}</div>
    <div className="detail-grid">
      <div className="detail-main">
        {ticket.sla_policy_id && <section className="panel ticket-sla-panel"><div className="section-heading"><div className="mini-icon"><Clock3 size={17} /></div><div><strong>Suivi du SLA</strong><span>Échéances calculées à partir de l’ouverture du ticket</span></div></div><div className="ticket-sla-dates"><div><span>Contournement attendu</span><strong>{ticket.workaround_due_at ? date(ticket.workaround_due_at) : 'Non défini'}</strong>{ticket.workaround_at && <small>Appliqué le {date(ticket.workaround_at)}</small>}</div><div><span>Résolution attendue</span><strong>{ticket.resolution_due_at ? date(ticket.resolution_due_at) : 'Non définie'}</strong></div><div><span>Pénalité support</span><strong>{ticket.sla_support_units_per_hour} U / heure de dépassement</strong></div>{ticket.customer_penalty_units > 0 && <div><span>Pénalité client constatée</span><strong>{ticket.customer_penalty_units} U{ticket.customer_penalty_reason ? ` · ${ticket.customer_penalty_reason}` : ''}</strong></div>}</div>{isStaff && <div className="ticket-sla-actions">{!ticket.workaround_at && <button className="button secondary" disabled={busy} onClick={markWorkaround}>Enregistrer le contournement</button>}{ticket.customer_penalty_units <= 0 && <form onSubmit={recordCompliance}><label>Constat client<select aria-label="Constat client" required value={complianceFinding} onChange={(event) => setComplianceFinding(event.target.value)}><option value="">Sélectionner un constat…</option><option value="nonconforming">Ticket non conforme</option><option value="no_defect">Aucun défaut constaté</option></select></label><label>Justification<input aria-label="Justification du constat" required minLength={5} maxLength={2000} placeholder="Décrivez le motif (5 caractères minimum)" value={complianceNote} onChange={(event) => setComplianceNote(event.target.value)} /></label><button className="button secondary" disabled={busy || !complianceFinding}>Enregistrer le constat</button></form>}</div>}</section>}
        <section className="panel description-panel"><div className="section-heading"><div className="mini-icon"><FilePlus2 size={17} /></div><div><strong>Description du problème</strong><span>Soumis par {ticket.customer_name}</span></div></div><p className="description-text">{ticket.description}</p></section>
        <section className="panel ai-panel">
          <div className="ai-title"><span className="ai-logo"><Sparkles size={17} /></span><div><strong>{ticket.status === 'ai_triage' ? 'Diagnostic guidé par IA' : 'Diagnostic initial IA'}</strong><span>Propulsé par {ticket.ai_provider || 'le fournisseur IA configuré'}</span></div><span className={`analysis-state ${ticket.ai_status}`}>{ticket.ai_status === 'complete' ? <><Check size={13} /> Réponse prête</> : ticket.ai_status === 'error' ? <><AlertCircle size={13} /> Indisponible</> : <><LoaderCircle size={13} className="ai-thinking-spinner" /> Réflexion…</>}</span></div>
          {isAiThinking && <div className="ai-thinking-banner" role="status" aria-live="polite"><LoaderCircle size={16} className="ai-thinking-spinner" /><span>L’assistant analyse votre message et prépare la suite du diagnostic.</span></div>}
          {ticket.ai_summary && <p className="ai-summary">{ticket.ai_summary}</p>}
          {ticket.resolution_note && <div className="ticket-outcome"><strong>{ticket.status === 'resolved' ? 'Résolution proposée' : 'Résolution précédente'}</strong><p>{ticket.resolution_note}</p></div>}
          {ticket.rejection_reason && <div className="ticket-outcome rejected"><strong>Motif du rejet</strong><p>{ticket.rejection_reason}</p></div>}
          {ticket.ai_recommendations?.length > 0 && <div className="recommendations"><strong>Actions et éléments à vérifier</strong><ol>{ticket.ai_recommendations.map((item, index) => <li key={index}>{item}</li>)}</ol></div>}
          {ticket.ai_escalation_recommended && ticket.status === 'ai_triage' && <div className="alert warning"><ArrowUpRight size={16} /> L’assistant recommande de passer la main à un agent support.</div>}
          {ticket.ai_status === 'error' && <div className="ai-error"><AlertCircle size={16} /><span>L’IA n’a pas pu répondre. Votre ticket n’est pas perdu : vous pouvez le transmettre au support.{isStaff && ticket.ai_error && <small>{ticket.ai_error}</small>}</span></div>}
          {!isStaff && ticket.status === 'ai_triage' && <AIModelPicker options={aiOptions} value={ticket.ai_preferences} onChange={selectCustomerModel} disabled={ticket.ai_status === 'pending'} label="Modèle IA pour le diagnostic" />}
          {!isStaff && ticket.status === 'ai_triage' && (hasRepliedToFirstAi
            ? <button className="button secondary handoff-button" disabled={busy} onClick={handoff}><ArrowUpRight size={16} />Transférer la conversation à un agent</button>
            : <p className="handoff-hint">Vous pourrez transférer la conversation à un agent après avoir répondu à la première demande de l’assistant.</p>)}
          {!isStaff && ticket.status === 'resolved' && <button className="button primary handoff-button" disabled={busy} onClick={closeTicket}><Check size={16} />Confirmer et clôturer la demande</button>}
        </section>
        <section className="panel conversation-panel"><div className="panel-head"><div><h2>{ticket.status === 'ai_triage' && !isStaff ? 'Conversation de diagnostic' : 'Conversation du ticket'}</h2><p>{ticket.status === 'ai_triage' && !isStaff ? 'L’assistant propose une étape à la fois. Répondez ou ajoutez les éléments demandés.' : 'L’agent dispose de cet échange complet pour poursuivre le traitement.'}</p></div><span className="conversation-count"><MessageCircle size={14} />{ticket.messages.length}</span></div>
          <div className="messages">{ticket.messages.map((item) => <article key={item.id} className={`message ${item.author_type === 'ai' ? 'assistant' : item.author_id === user.id ? 'own' : ''}`}><Avatar name={item.author_name} size="small" /><div className="message-content"><div className="message-meta"><strong>{item.author_name}</strong>{item.visibility === 'internal' && <span className="internal-tag">Note interne</span>}<time>{date(item.created_at)}</time></div><MessageBody body={item.body} isMarkdown={item.author_type === 'ai'} /></div></article>)}{isThreadAiThinking && <div className="message assistant ai-thinking" role="status" aria-live="polite"><Avatar name="Assistant IA" size="small" /><div className="message-content"><div className="message-meta"><strong>Assistant IA</strong></div><div className="ai-thinking-copy"><LoaderCircle size={15} className="ai-thinking-spinner" /><span>{isStaff ? 'L’assistant prépare une réponse pour le support…' : 'L’assistant réfléchit à votre réponse…'}</span><span className="ai-thinking-dots" aria-hidden="true"><i /><i /><i /></span></div></div></div>}{ticket.messages.length === 0 && !isThreadAiThinking && <div className="empty-state compact">Aucun message pour l’instant.</div>}</div>
          {!['closed', 'rejected'].includes(ticket.status) && <form className="message-form" onSubmit={sendMessage}>
            {isStaff && <label className="internal-toggle"><input type="checkbox" checked={internal} onChange={(e) => setInternal(e.target.checked)} /> Note interne</label>}
            <div className="message-compose"><textarea rows="2" placeholder={isStaff ? (internal ? 'Écrire une note visible uniquement par l’équipe…' : 'Répondre au client…') : ticket.status === 'resolved' ? 'Répondre pour rouvrir cette demande…' : 'Répondre à l’assistant ou à l’agent…'} value={message} onChange={(e) => setMessage(e.target.value)} /><button className="button primary send-button" disabled={busy || (!message.trim() && files.length === 0) || (!isStaff && ticket.ai_status === 'pending')}><Send size={16} />{busy ? 'Envoi…' : 'Envoyer'}</button></div>
            <label className="attachment-picker"><Paperclip size={15} />Joindre des fichiers<input type="file" multiple accept=".csv,.jpg,.jpeg,.log,.pdf,.pcap,.png,.txt,.webp,.zip" onChange={(event) => setFiles(Array.from(event.target.files || []))} /><span>{files.length ? files.map((file) => file.name).join(', ') : 'Journaux, captures, export de diagnostic (5 fichiers, 10 Mo max. par fichier)'}</span></label>
            {isStaff && <span className="field-hint">{internal ? 'Cette note ne sera pas visible par le client.' : 'Votre réponse sera visible par le client.'}</span>}
          </form>}
        </section>
        {ticket.attachments.length > 0 && <section className="panel attachment-panel"><div className="panel-head"><div><h2>Pièces jointes</h2><p>Transmises avec la conversation à l’équipe de support.</p></div><Paperclip size={17} className="muted-icon" /></div><div className="attachment-list">{ticket.attachments.map((attachment) => <button className="attachment-row" key={attachment.id} onClick={() => downloadAttachment(attachment)}><FilePlus2 size={16} /><span><strong>{attachment.file_name}</strong><small>{Math.ceil(attachment.size / 1024)} Ko · {attachment.uploader_name} · {date(attachment.created_at)}</small></span><ArrowUpRight size={15} /></button>)}</div></section>}
      </div>
      <aside className="detail-aside">
        <section className="panel aside-panel"><h3>Suivi de la demande</h3><div className="timeline">{ticket.events.map((event) => <div className="timeline-item" key={event.id}><span className={`timeline-mark ${event.event_type}`}><TimelineIcon type={event.event_type} /></span><div><strong>{eventLabel(event)}</strong><span>{event.actor_name}</span><time>{date(event.created_at)}</time>{event.event_type === 'escalated' && event.details?.team && <small>Équipe : {event.details.team}{event.details.external_reference ? ` · ${event.details.external_reference}` : ''}</small>}</div></div>)}</div></section>
        <section className="panel aside-panel requester-card"><h3>Demandeur</h3><div className="requester"><Avatar name={ticket.customer_name} /><div><strong>{ticket.customer_name}</strong><span>Client</span></div></div><div className="info-divider" /><div className="info-row"><span>Produit</span><strong>{ticket.product}</strong></div><div className="info-row"><span>Version</span><strong>{ticket.version || 'Non précisée'}</strong></div><div className="info-row"><span>Dernière activité</span><strong>{date(ticket.updated_at)}</strong></div></section>
        {isStaff && <section className="panel aside-panel"><h3>Prise en charge</h3><label className="compact-label">Agent assigné<select value={ticket.assigned_agent_id || ''} onChange={assign}><option value="">Non assigné</option>{assignable.map((agent) => <option key={agent.id} value={agent.id}>{agent.full_name}{agent.role === 'admin' ? ' (administrateur)' : ''}</option>)}</select></label></section>}
        {isStaff && <section className="panel aside-panel agent-ai-panel"><h3><Sparkles size={16} /> Aide IA privée</h3><p>L’IA reçoit le ticket et toute la conversation, ainsi que la liste des fichiers. Elle ne lit pas le contenu des pièces jointes. Sa réponse apparaît comme note interne.</p><AIModelPicker options={aiOptions} value={agentChoice} onChange={setAgentChoice} disabled={aiBusy} label="Modèle IA" /><button type="button" className="button secondary full" disabled={aiBusy} onClick={requestSummary}><Sparkles size={15} />Résumer le ticket</button>{aiOptions?.kb_enabled && <button type="button" className="button secondary full" disabled={aiBusy} onClick={requestKbSearch}><BookOpen size={15} />Chercher dans la KB constructeur</button>}<form onSubmit={requestAiHelp}><label className="compact-label">Votre question<textarea required minLength={3} maxLength={2000} rows="3" placeholder="Ex. Quelles vérifications sûres proposer ensuite ?" value={aiQuestion} onChange={(event) => setAiQuestion(event.target.value)} /></label><button className="button secondary full" disabled={aiBusy || !aiQuestion.trim()}><Sparkles size={15} />{aiBusy ? 'L’IA analyse le dossier…' : 'Demander à l’IA'}</button></form></section>}
        {isStaff && aiBusy && <div className="ai-thinking-banner" role="status" aria-live="polite"><LoaderCircle size={16} className="ai-thinking-spinner" /><span>L’assistant analyse le ticket; sa réponse apparaîtra dans les notes internes.</span></div>}
        {ticket.status === 'escalated' && <section className="panel aside-panel escalation-current"><div className="escalation-icon"><ArrowUpRight size={17} /></div><h3>Incident escaladé</h3><p>En cours de traitement par <strong>{ticket.escalation_team}</strong></p>{ticket.external_reference && <span className="external-ref">Référence : {ticket.external_reference}</span>}</section>}
      </aside>
    </div>
  </div>
}

function TimelineIcon({ type }) {
  if (type === 'created') return <Plus size={12} />
  if (type === 'status_changed') return <Activity size={12} />
  if (type === 'escalated') return <ArrowUpRight size={12} />
  if (type === 'ai_handoff') return <ArrowUpRight size={12} />
  if (type.startsWith('ai_')) return <Sparkles size={12} />
  if (type === 'message_added') return <MessageCircle size={12} />
  return <Check size={12} />
}
function eventLabel(event) {
  if (event.event_type === 'created') return 'Incident créé'
  if (event.event_type === 'status_changed') return `Statut modifié : ${statuses[event.details?.to]?.[0] || event.details?.to}`
  if (event.event_type === 'customer_closed') return 'Demande clôturée par le client'
  if (event.event_type === 'ticket_reopened') return `Ticket rouvert : ${statuses[event.details?.to]?.[0] || event.details?.to}`
  if (event.event_type === 'escalated') return 'Incident escaladé'
  if (event.event_type === 'ai_handoff') return 'Transmis à la file de support'
  if (event.event_type === 'ai_model_selected') return `Modèle IA choisi : ${event.details?.model || ''}${event.details?.think ? ' (raisonnement)' : ''}`
  if (event.event_type === 'ai_response') return 'Réponse de l’assistant IA'
  if (event.event_type === 'ai_agent_assist') return 'Réponse de l’assistance IA privée'
  if (event.event_type === 'ai_agent_assist_failed') return 'Assistance IA privée indisponible'
  if (event.event_type === 'ai_handoff_summary') return 'Résumé IA de passation ajouté en note interne'
  if (event.event_type === 'ai_handoff_summary_failed') return 'Résumé IA de passation indisponible'
  if (event.event_type === 'attachments_added') return `${event.details?.count || ''} pièce(s) jointe(s) ajoutée(s)`
  if (event.event_type === 'message_added') return event.details?.visibility === 'internal' ? 'Note interne ajoutée' : 'Message ajouté'
  if (event.event_type === 'ai_analysis_completed') return 'Analyse IA terminée'
  if (event.event_type === 'ai_analysis_failed') return 'Analyse IA indisponible'
  if (event.event_type === 'assignment_changed') return event.details?.agent_name ? `Assigné à ${event.details.agent_name}` : 'Assignation retirée'
  return 'Mise à jour'
}

function TeamPage({ staff, companies, token, user, onCreated, onUpdated }) {
  const [form, setForm] = useState({ full_name: '', email: '', password: '', role: 'agent' })
  const [error, setError] = useState('')
  const [success, setSuccess] = useState('')
  const [busy, setBusy] = useState(false)
  const [search, setSearch] = useState('')
  const [roleFilter, setRoleFilter] = useState('all')
  const [edits, setEdits] = useState({})
  const [passwords, setPasswords] = useState({})
  const [busyUser, setBusyUser] = useState('')
  const [userNotice, setUserNotice] = useState('')
  const isAdmin = user.role === 'admin'
  const visibleUsers = staff.filter((member) => {
    const term = search.trim().toLowerCase()
    return (roleFilter === 'all' || member.role === roleFilter)
      && (!term || `${member.full_name} ${member.email} ${member.company_name || ''}`.toLowerCase().includes(term))
  })

  const pager = usePagination(visibleUsers, { storageKey: 'users', initialSize: 10, resetKey: `${search}|${roleFilter}` })

  function updateEdit(member, field, value) {
    const current = edits[member.id] || {}
    setEdits((items) => ({ ...items, [member.id]: { ...current, [field]: value } }))
  }

  function updateRole(member, role) {
    setEdits((items) => {
      const next = { ...(items[member.id] || {}), role }
      if (role !== 'customer') delete next.company_id
      if (role !== 'agent') delete next.company_ids
      return { ...items, [member.id]: next }
    })
  }

  async function saveUser(member) {
    const changes = edits[member.id] || {}
    if (!Object.keys(changes).length) return
    setBusyUser(member.id); setError(''); setUserNotice('')
    try {
      const updated = await request(`/users/${member.id}`, {
        token, method: 'PATCH', body: JSON.stringify(changes),
      })
      onUpdated(updated)
      setEdits((items) => { const next = { ...items }; delete next[member.id]; return next })
      setUserNotice(`Compte de ${updated.full_name} mis à jour.`)
    } catch (err) { setError(err.message) } finally { setBusyUser('') }
  }

  async function resetPassword(member) {
    const temporaryPassword = passwords[member.id] || ''
    if (temporaryPassword.length < 10) {
      setError('Le mot de passe temporaire doit contenir au moins 10 caractères.')
      return
    }
    setBusyUser(member.id); setError(''); setUserNotice('')
    try {
      await request(`/users/${member.id}/password`, {
        token, method: 'PUT', body: JSON.stringify({ temporary_password: temporaryPassword }),
      })
      setPasswords((items) => ({ ...items, [member.id]: '' }))
      setUserNotice(`Mot de passe temporaire réinitialisé pour ${member.full_name}. Transmettez-le au compte par un canal sûr.`)
    } catch (err) { setError(err.message) } finally { setBusyUser('') }
  }

  async function submit(event) {
    event.preventDefault()
    setBusy(true); setError(''); setSuccess('')
    try {
      const member = await request('/users', { token, method: 'POST', body: JSON.stringify(form) })
      onCreated(member)
      setForm({ full_name: '', email: '', password: '', role: 'agent' })
      setSuccess('Le compte collaborateur a été créé.')
    } catch (err) { setError(err.message) } finally { setBusy(false) }
  }
  return <div className="team-page">
    <div className="welcome-row"><div><span className="eyebrow">GESTION DES COMPTES</span><h1>Utilisateurs</h1><p>{isAdmin ? 'Gérez tous les comptes du portail, leurs accès et leurs paramètres.' : 'Gérez les comptes clients et agents des entreprises auxquelles vous êtes affecté.'}</p></div></div>
    {error && <div className="alert error page-alert">{error}<button onClick={() => setError('')}>Fermer</button></div>}
    {userNotice && <div className="alert success page-alert"><Check size={16} />{userNotice}</div>}
    <div className="team-grid">
      <section className="panel user-management-panel">
        <div className="panel-head"><div><h2>{isAdmin ? 'Tous les utilisateurs' : 'Utilisateurs des entreprises'}</h2><p>{visibleUsers.length} sur {staff.length} compte{staff.length === 1 ? '' : 's'} dans votre périmètre</p></div><Users size={19} className="muted-icon" /></div>
        <div className="user-management-tools"><input aria-label="Rechercher un utilisateur" value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Rechercher un nom, e-mail ou entreprise…" /><select aria-label="Filtrer par rôle" value={roleFilter} onChange={(event) => setRoleFilter(event.target.value)}><option value="all">Tous les rôles</option><option value="customer">Clients</option><option value="agent">Agents</option>{isAdmin && <option value="admin">Administrateurs</option>}<option value="system">Système</option></select></div>
        <div className="user-admin-list">{pager.items.map((member) => {
          const change = edits[member.id] || {}
          const managed = member.role !== 'system' && (isAdmin || member.id !== user.id)
          const roles = ['customer', 'agent', 'admin']
          return <article className="user-admin-card" key={member.id}>
            <div className="user-admin-heading"><Avatar name={member.full_name} /><div className="user-admin-identity"><strong>{member.full_name}</strong><span>{member.email}{member.company_name ? ` · ${member.company_name}` : ''}</span></div><span className={`user-active-state ${member.is_active ? 'active' : 'inactive'}`}>{member.is_active ? 'Actif' : 'Désactivé'}</span></div>
            {!managed ? <p className="field-hint">Votre compte ne peut pas être modifié depuis cette liste.</p> : <>
              <div className={`user-admin-fields ${isAdmin ? 'admin' : ''}`}>
                <label>Nom complet<input value={change.full_name ?? member.full_name} onChange={(event) => updateEdit(member, 'full_name', event.target.value)} /></label>
                <label>E-mail<input type="email" value={change.email ?? member.email} onChange={(event) => updateEdit(member, 'email', event.target.value)} /></label>
                <label className="user-active-choice"><input type="checkbox" checked={change.is_active ?? member.is_active} onChange={(event) => updateEdit(member, 'is_active', event.target.checked)} />Compte actif</label>
                {isAdmin && <label>Rôle<select value={change.role ?? member.role} onChange={(event) => updateRole(member, event.target.value)}>{roles.map((role) => <option key={role} value={role}>{role === 'customer' ? 'Client' : role === 'agent' ? 'Agent' : 'Administrateur'}</option>)}</select></label>}
                {isAdmin && member.role !== 'system' && <label>État du compte<select value={change.account_status ?? member.account_status} onChange={(event) => updateEdit(member, 'account_status', event.target.value)}><option value="approved">Approuvé</option><option value="pending">En attente</option><option value="rejected">Refusé</option></select></label>}
                {isAdmin && (change.role ?? member.role) === 'customer' && <label>Entreprise<select value={Object.hasOwn(change, 'company_id') ? (change.company_id ?? '') : (member.company_id ?? '')} onChange={(event) => updateEdit(member, 'company_id', event.target.value || null)}><option value="">Aucune entreprise</option>{companies.map((company) => <option key={company.id} value={company.id}>{company.name}</option>)}</select></label>}
                {isAdmin && (change.role ?? member.role) === 'agent' && <label>Entreprises affectées<select multiple value={change.company_ids ?? member.company_ids ?? []} onChange={(event) => updateEdit(member, 'company_ids', Array.from(event.target.selectedOptions, (option) => option.value))}>{companies.map((company) => <option key={company.id} value={company.id}>{company.name}</option>)}</select></label>}
                {isAdmin && member.role === 'customer' && <div className="user-consent-controls"><label><input type="checkbox" checked={change.ai_processing_consent ?? member.ai_processing_consent} onChange={(event) => updateEdit(member, 'ai_processing_consent', event.target.checked)} />Traitement IA</label><label><input type="checkbox" checked={change.ai_trace_consent ?? member.ai_trace_consent} onChange={(event) => updateEdit(member, 'ai_trace_consent', event.target.checked)} />Traces d’évaluation</label></div>}
              </div>
              <div className="user-admin-actions">
                <button className="button primary" disabled={busyUser === member.id || !Object.keys(change).length} onClick={() => saveUser(member)}>{busyUser === member.id ? 'Enregistrement…' : 'Enregistrer les changements'}</button>
                <div className="password-reset-controls"><input type="password" autoComplete="new-password" minLength="10" value={passwords[member.id] || ''} onChange={(event) => setPasswords((items) => ({ ...items, [member.id]: event.target.value }))} placeholder="Mot de passe temporaire (10 caractères min.)" /><button className="button secondary" disabled={busyUser === member.id || (passwords[member.id] || '').length < 10} onClick={() => resetPassword(member)}>Réinitialiser le mot de passe</button></div>
              </div>
            </>}
          </article>
        })}</div>
        <Pagination {...pager} sizes={[5, 10, 25, 50]} label="comptes" />
      </section>
      {isAdmin && <section className="panel create-staff"><h2>Ajouter un collaborateur</h2><p className="muted">Créez un compte agent ou administrateur. Vous pourrez ensuite lui affecter une ou plusieurs entreprises.</p>{success && <div className="alert success"><Check size={16} />{success}</div>}<form className="form-stack" onSubmit={submit}><label>Nom complet<input required minLength="2" value={form.full_name} onChange={(e) => setForm({ ...form, full_name: e.target.value })} /></label><label>Adresse e-mail<input required type="email" value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} /></label><label>Mot de passe temporaire<input required type="password" minLength="10" value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} /></label><label>Rôle<select value={form.role} onChange={(e) => setForm({ ...form, role: e.target.value })}><option value="agent">Agent support</option><option value="admin">Administrateur</option></select></label><button className="button primary" disabled={busy}><Plus size={16} />{busy ? 'Création…' : 'Créer le compte'}</button></form></section>}
    </div>
  </div>
}

function CompaniesPage({ companies, staff, user, token, onCreated, onUpdated }) {
  const [name, setName] = useState('')
  const [agentIds, setAgentIds] = useState([])
  const [invitation, setInvitation] = useState(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const agents = staff.filter((member) => member.role === 'agent' && member.is_active)
  if (user.role === 'agent' && !agents.some((agent) => agent.id === user.id)) agents.push(user)
  const pager = usePagination(companies, { storageKey: 'companies', initialSize: 10 })

  async function submit(event) {
    event.preventDefault()
    setBusy(true)
    setError('')
    setInvitation(null)
    try {
      const company = await request('/companies', {
        token,
        method: 'POST',
        body: JSON.stringify({ name, agent_ids: agentIds }),
      })
      onCreated(company)
      setInvitation({ companyId: company.id, code: company.invitation_code })
      setName('')
      setAgentIds([])
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  function toggleAgent(agentId) {
    setAgentIds((current) => current.includes(agentId)
      ? current.filter((id) => id !== agentId)
      : [...current, agentId])
  }

  async function regenerateCode(companyId) {
    setBusy(true)
    setError('')
    try {
      const company = await request(`/companies/${companyId}/invitation-code`, { token, method: 'POST' })
      onUpdated(company)
      setInvitation({ companyId: company.id, code: company.invitation_code })
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  return <div className="team-page">
    <div className="welcome-row"><div><span className="eyebrow">GESTION CLIENTS</span><h1>Entreprises clientes</h1><p>Créez un espace client, affectez son équipe support et transmettez son code d’accès.</p></div></div>
    <div className="team-grid">
      <section className="panel">
        <div className="panel-head"><div><h2>Entreprises suivies</h2><p>{companies.length} entreprise{companies.length === 1 ? '' : 's'}</p></div><Users size={19} className="muted-icon" /></div>
        <div className="staff-list">
          {companies.length === 0 ? <div className="empty-state compact">Aucune entreprise n’est encore affectée à votre équipe.</div> : pager.items.map((company) => (
            <div className="staff-row company-row" key={company.id}>
              <span className="company-mark"><Headset size={16} /></span>
              <div><strong>{company.name}{company.is_individual && <span className="badge blue individual-badge">Indépendant</span>}</strong><span>{company.agent_ids.length} agent{company.agent_ids.length === 1 ? '' : 's'} affecté{company.agent_ids.length === 1 ? '' : 's'}</span></div>
              {company.is_individual ? <span /> : <button className="button secondary" disabled={busy} onClick={() => regenerateCode(company.id)}>Nouveau code</button>}
            </div>
          ))}
        </div>
        <Pagination {...pager} sizes={[5, 10, 25, 50]} label="entreprises" />
        {invitation && <div className="invite-code"><strong>Code d’accès pour {companies.find((company) => company.id === invitation.companyId)?.name}</strong><code>{invitation.code}</code><span>Transmettez-le au contact client par un canal sécurisé. Le code précédent est maintenant révoqué.</span></div>}
      </section>
      <section className="panel create-staff">
        <h2>Créer une entreprise</h2>
        <p className="muted">Le code d’accès est affiché une seule fois. Transmettez-le au contact client par un canal sécurisé.</p>
        {error && <div className="alert error">{error}</div>}
        <form className="form-stack" onSubmit={submit}>
          <label>Nom de l’entreprise<input required minLength="2" maxLength="180" value={name} onChange={(event) => setName(event.target.value)} /></label>
          <fieldset className="agent-picker"><legend>Agents support affectés</legend>
            {agents.length === 0 ? <p className="field-hint">Aucun agent support disponible. Un administrateur doit d’abord créer les comptes agents.</p> : agents.map((agent) => (
              <label key={agent.id}><input type="checkbox" checked={agentIds.includes(agent.id)} onChange={() => toggleAgent(agent.id)} />{agent.full_name}</label>
            ))}
          </fieldset>
          <button className="button primary" disabled={busy || agents.length === 0}><Plus size={16} />{busy ? 'Création…' : 'Créer l’entreprise'}</button>
        </form>
      </section>
    </div>
  </div>
}

function AccountRequestsPage({ requests, companies, user, token, onDecision, onUpdated }) {
  const [error, setError] = useState('')
  const [busyId, setBusyId] = useState('')
  const [companySelections, setCompanySelections] = useState({})
  const pager = usePagination(requests, { storageKey: 'requests', initialSize: 10 })

  async function decide(userId, decision) {
    setError('')
    setBusyId(userId)
    try {
      await request(`/users/${userId}/approval`, {
        token,
        method: 'PATCH',
        body: JSON.stringify({ decision }),
      })
      onDecision(userId)
    } catch (err) {
      setError(err.message)
    } finally {
      setBusyId('')
    }
  }

  async function assignCompany(userId) {
    const companyId = companySelections[userId]
    if (!companyId) return
    setError('')
    setBusyId(userId)
    try {
      const customer = await request(`/users/${userId}/company`, {
        token,
        method: 'PATCH',
        body: JSON.stringify({ company_id: companyId }),
      })
      onUpdated(customer)
    } catch (err) {
      setError(err.message)
    } finally {
      setBusyId('')
    }
  }

  return <div className="team-page">
    <div className="welcome-row"><div><span className="eyebrow">VALIDATION DES ACCÈS</span><h1>Demandes de comptes clients</h1><p>Vérifiez l’entreprise et l’identité avant d’autoriser la création d’incidents.</p></div></div>
    {error && <div className="alert error page-alert">{error}</div>}
    <section className="panel">
      <div className="panel-head"><div><h2>En attente de validation</h2><p>{requests.length} demande{requests.length === 1 ? '' : 's'}</p></div><CheckCheck size={19} className="muted-icon" /></div>
      {requests.length === 0 ? <div className="empty-state compact">Aucune demande d’accès en attente.</div> : <div className="approval-list">
        {pager.items.map((customer) => (
          <div className="approval-row" key={customer.id}>
            <Avatar name={customer.full_name} />
            <div className="approval-identity"><strong>{customer.full_name}</strong><span>{customer.email}</span></div>
            <div className="approval-company"><strong>{customer.company_name || 'Entreprise à définir'}{customer.is_individual && <span className="badge blue individual-badge">Indépendant</span>}</strong><span>Demandé le {customer.created_at ? date(customer.created_at) : '—'}</span></div>
            <div className="approval-actions">
              {!customer.company_id && user.role === 'admin' ? <>
                <select aria-label={`Entreprise pour ${customer.full_name}`} value={companySelections[customer.id] || ''} onChange={(event) => setCompanySelections((current) => ({ ...current, [customer.id]: event.target.value }))}>
                  <option value="">Rattacher à une entreprise…</option>
                  {companies.map((company) => <option key={company.id} value={company.id}>{company.name}</option>)}
                </select>
                <button className="button primary" disabled={busyId === customer.id || !companySelections[customer.id]} onClick={() => assignCompany(customer.id)}>Rattacher</button>
                <button className="button secondary" disabled={busyId === customer.id} onClick={() => decide(customer.id, 'reject')}>Refuser</button>
              </> : customer.company_id ? <>
                <button className="button primary" disabled={busyId === customer.id} onClick={() => decide(customer.id, 'approve')}><Check size={15} />Approuver</button>
                <button className="button secondary" disabled={busyId === customer.id} onClick={() => decide(customer.id, 'reject')}>Refuser</button>
              </> : <span className="field-hint">Rattachement requis par l’admin</span>}
            </div>
          </div>
        ))}
        <Pagination {...pager} sizes={[5, 10, 25, 50]} label="demandes" />
      </div>}
    </section>
  </div>
}

function AIRoutingPage({ token }) {
  const [config, setConfig] = useState(null)
  const [tasks, setTasks] = useState({})
  const [roles, setRoles] = useState({})
  const [providers, setProviders] = useState([])
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')

  const loadConfig = useCallback(async () => {
    setLoading(true)
    try {
      const data = await request('/admin/ai-routing', { token })
      setConfig({ enabled: data.enabled, routes: data.routes })
      setTasks(data.tasks)
      setRoles(data.roles)
      setProviders(data.providers)
      setError('')
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }, [token])

  useEffect(() => { loadConfig() }, [loadConfig])

  function updateRoute(task, role, update) {
    setConfig((current) => ({
      ...current,
      routes: {
        ...current.routes,
        [task]: {
          ...current.routes[task],
          [role]: update(current.routes[task][role]),
        },
      },
    }))
  }

  function defaultChoice(providerId) {
    const provider = providers.find((item) => item.id === providerId && item.configured)
      || providers.find((item) => item.configured)
    return provider ? { provider: provider.id, model: provider.default_model, think: Boolean(provider.default_think), timeout_seconds: provider.default_timeout_seconds || 120 } : null
  }

  function toggleRoute(task, role, enabled) {
    updateRoute(task, role, (route) => ({
      ...route,
      enabled,
      models: enabled && route.models.length === 0 && defaultChoice('ollama')
        ? [defaultChoice('ollama')]
        : route.models,
    }))
  }

  function addModel(task, role) {
    const choice = defaultChoice('ollama')
    if (!choice) {
      setError('Aucun fournisseur LLM n’est configuré côté serveur.')
      return
    }
    updateRoute(task, role, (route) => {
      if (route.models.length >= 4) return route
      // Propose en priorité un modèle détecté qui n'est pas déjà dans la règle.
      const provider = providers.find((item) => item.id === choice.provider)
      const used = new Set(route.models.filter((item) => item.provider === choice.provider).map((item) => item.model))
      const next = (provider?.available_models || []).find((name) => !used.has(name))
      const added = next
        ? { ...choice, model: next, think: next === provider.default_model ? choice.think : false }
        : choice
      return { ...route, models: [...route.models, added] }
    })
  }

  function updateModel(task, role, index, field, value) {
    updateRoute(task, role, (route) => ({
      ...route,
      models: route.models.map((choice, modelIndex) => {
        if (modelIndex !== index) return choice
        if (field === 'provider') {
          const provider = providers.find((item) => item.id === value)
          return { ...choice, provider: value, model: provider?.default_model || '', think: Boolean(provider?.default_think), timeout_seconds: provider?.default_timeout_seconds || 120 }
        }
        return { ...choice, model: value }
      }),
    }))
  }

  function removeModel(task, role, index) {
    updateRoute(task, role, (route) => {
      const models = route.models.filter((_choice, modelIndex) => modelIndex !== index)
      return { ...route, models, enabled: models.length ? route.enabled : false }
    })
  }

  function moveModel(task, role, index, direction) {
    updateRoute(task, role, (route) => {
      const destination = index + direction
      if (destination < 0 || destination >= route.models.length) return route
      const models = [...route.models]
      const moved = models[index]
      models[index] = models[destination]
      models[destination] = moved
      return { ...route, models }
    })
  }

  async function resetDefaults() {
    if (!window.confirm('Rétablir le modèle, le raisonnement et le délai par défaut du serveur pour toutes les règles ?')) return
    setSaving(true)
    setError('')
    setNotice('')
    try {
      const data = await request('/admin/ai-routing', { token, method: 'DELETE' })
      setConfig({ enabled: data.enabled, routes: data.routes })
      setProviders(data.providers)
      setNotice('La configuration par défaut a été rétablie.')
    } catch (err) {
      setError(err.message)
    } finally {
      setSaving(false)
    }
  }

  async function save(event) {
    event.preventDefault()
    setSaving(true)
    setError('')
    setNotice('')
    try {
      const saved = await request('/admin/ai-routing', {
        token,
        method: 'PUT',
        body: JSON.stringify(config),
      })
      setConfig((current) => ({ ...current, ...saved }))
      setNotice('La configuration des tâches IA a été enregistrée.')
    } catch (err) {
      setError(err.message)
    } finally {
      setSaving(false)
    }
  }

  if (loading && !config) return <div className="empty-state">Chargement de la configuration IA…</div>

  return <div className="ai-routing-page">
    <div className="welcome-row"><div><span className="eyebrow">ADMINISTRATION IA</span><h1>Configuration des modèles IA</h1><p>Affectez des LLM par tâche et par public, définissez un ordre de priorité et basculez l’assistance générale sans perdre vos règles.</p></div><button className="button secondary" disabled={loading} onClick={loadConfig}><RefreshCw size={16} />Actualiser</button></div>
    {error && <div className="alert error page-alert"><AlertCircle size={17} />{error}</div>}
    {notice && <div className="alert success page-alert"><Check size={16} />{notice}</div>}
    {!config ? <div className="empty-state">La configuration IA n’est pas disponible.</div> : <form onSubmit={save}>
      <section className={`panel ai-master-toggle ${config.enabled ? 'enabled' : 'paused'}`}>
        <div className="ai-master-icon"><BrainCircuit size={21} /></div>
        <div className="ai-master-copy"><strong>Assistance IA générale</strong><span>{config.enabled ? 'Les règles ci-dessous déterminent les modèles utilisés.' : 'En pause : aucun appel LLM ne sera lancé. Vos affectations sont conservées.'}</span></div>
        <label className="ai-toggle"><input type="checkbox" checked={config.enabled} onChange={(event) => setConfig((current) => ({ ...current, enabled: event.target.checked }))} /><span>{config.enabled ? 'Activée' : 'En pause'}</span></label>
        <small className="ai-master-note">Au moins une règle activée avec un modèle doit rester configurée, même pendant la pause générale.</small>
      </section>

      <section className="panel ai-provider-panel">
        <div className="panel-head"><div><h2>Fournisseurs disponibles</h2><p>Les identifiants de modèles sont modifiables; les modèles listés sont ceux détectés par le service.</p></div><Database size={19} className="muted-icon" /></div>
        <div className="ai-provider-list">{providers.map((provider) => {
          const providerStatus = {
            healthy: ['Service actif', 'green'],
            warning: ['Aucun modèle détecté', 'amber'],
            down: ['Service indisponible', 'red'],
            configured: ['Clé configurée', 'green'],
            unconfigured: ['Non configuré', 'gray'],
          }
          const [statusLabel, statusColor] = providerStatus[provider.status] || ['État inconnu', 'gray']
          return <div className="ai-provider-row" key={provider.id}>
            <div><strong>{provider.label}</strong><span>{provider.configured ? `Modèle par défaut : ${provider.default_model}` : 'Non configuré — vérifiez les paramètres API.'}</span>{provider.id === 'anthropic' && provider.configured && <small>La connectivité sera confirmée lors du premier appel.</small>}{provider.error && <small>{provider.error}</small>}</div>
            <Badge value={statusLabel} map={{ [statusLabel]: [statusLabel, statusColor] }} />
          </div>
        })}</div>
      </section>

      <div className="ai-task-list">{Object.entries(tasks).map(([task, taskLabel]) => <section className="panel ai-task-panel" key={task}>
        <div className="panel-head"><div><h2>{taskLabel}</h2><p>Choisissez si la tâche est autorisée pour chaque type de personne et ordonnez ses modèles.</p></div><Sparkles size={18} className="muted-icon" /></div>
        <div className="ai-task-routes">{Object.entries(roles).map(([role, roleLabel]) => {
          const route = config.routes[task][role]
          return <div className={`ai-route-row ${route.enabled ? '' : 'disabled'}`} key={role}>
            <div className="ai-route-heading">
              <label className="ai-route-toggle"><input type="checkbox" checked={route.enabled} onChange={(event) => toggleRoute(task, role, event.target.checked)} /><strong>{roleLabel}</strong></label>
              <button type="button" className="button secondary ai-add-model" disabled={!route.enabled || route.models.length >= 4} onClick={() => addModel(task, role)}><Plus size={14} />Ajouter un modèle</button>
            </div>
            {!route.enabled ? <span className="field-hint">Cette tâche est désactivée pour ce public.</span> : route.models.length === 0 ? <span className="ai-validation-warning">Affectez au moins un modèle à cette règle.</span> : <div className="ai-model-list">
              {route.models.map((choice, index) => {
                const provider = providers.find((item) => item.id === choice.provider)
                const detectedModels = provider?.available_models || []
                const isCustomModel = !detectedModels.includes(choice.model)
                return <div className="ai-model-item" key={`${choice.provider}-${index}`}>
                <div className="ai-model-row">
                  <span className="ai-priority">{index + 1}</span>
                  <select aria-label={`Fournisseur prioritaire ${index + 1} pour ${taskLabel} (${roleLabel})`} value={choice.provider} onChange={(event) => updateModel(task, role, index, 'provider', event.target.value)}>
                    {providers.map((item) => <option key={item.id} value={item.id} disabled={!item.configured}>{item.label}{item.configured ? '' : ' (non configuré)'}</option>)}
                  </select>
                  <div className="ai-model-name">
                    {detectedModels.length > 0 && <select aria-label={`Modèle ${index + 1} pour ${taskLabel} (${roleLabel})`} value={isCustomModel ? '__custom__' : choice.model} onChange={(event) => updateModel(task, role, index, 'model', event.target.value === '__custom__' ? '' : event.target.value)}>
                      {detectedModels.map((model) => <option key={model} value={model}>{model}</option>)}
                      <option value="__custom__">Autre modèle…</option>
                    </select>}
                    {(isCustomModel || detectedModels.length === 0) && <input aria-label={`Identifiant du modèle ${index + 1} pour ${taskLabel} (${roleLabel})`} value={choice.model} maxLength={200} onChange={(event) => updateModel(task, role, index, 'model', event.target.value)} placeholder="Identifiant du modèle" />}
                  </div>
                  <button type="button" className="icon-button" title="Monter dans la priorité" aria-label="Monter dans la priorité" disabled={index === 0} onClick={() => moveModel(task, role, index, -1)}><ChevronUp size={16} /></button>
                  <button type="button" className="icon-button" title="Descendre dans la priorité" aria-label="Descendre dans la priorité" disabled={index === route.models.length - 1} onClick={() => moveModel(task, role, index, 1)}><ChevronDown size={16} /></button>
                  <button type="button" className="icon-button ai-remove-model" title="Retirer le modèle" aria-label="Retirer le modèle" onClick={() => removeModel(task, role, index)}><Trash2 size={15} /></button>
                </div>
                <div className="ai-model-options">
                  {choice.provider === 'ollama' && <label className="ai-option-check" title="Active le raisonnement interne des modèles « thinking » (qwen3, deepseek-r1…). Beaucoup plus lent sur CPU."><input type="checkbox" checked={Boolean(choice.think)} onChange={(event) => updateModel(task, role, index, 'think', event.target.checked)} />Raisonnement (think)</label>}
                  <label className="ai-option-timeout">Délai max (s)<input type="number" min={10} max={1800} step={10} value={choice.timeout_seconds ?? 120} onChange={(event) => updateModel(task, role, index, 'timeout_seconds', Math.min(1800, Math.max(10, Number(event.target.value) || 120)))} /></label>
                </div>
                </div>
              })}
            </div>}
          </div>
        })}</div>
      </section>)}</div>
      <div className="ai-save-bar"><span>Le premier modèle de chaque règle est le modèle par défaut; les clients et agents peuvent choisir parmi les modèles listés. Les changements sont appliqués immédiatement.</span><button type="button" className="button secondary" disabled={saving} onClick={resetDefaults}><RefreshCw size={16} />Rétablir les valeurs par défaut</button><button className="button primary" disabled={saving}><Check size={16} />{saving ? 'Enregistrement…' : 'Enregistrer la configuration'}</button></div>
    </form>}
  </div>
}

const helpSourceKinds = {
  doc: 'Corpus Alcatel',
  file: 'Fichier joint',
  company: 'Documents entreprise',
  kb: 'KB constructeur',
  web: 'Web',
}

function helpTime(value) {
  const moment = new Date(value)
  const sameDay = moment.toDateString() === new Date().toDateString()
  return new Intl.DateTimeFormat('fr-FR', sameDay ? { timeStyle: 'medium' } : { dateStyle: 'short', timeStyle: 'medium' }).format(moment)
}

function helpDuration(message) {
  if (!message.completed_at) return ''
  const seconds = (new Date(message.completed_at) - new Date(message.created_at)) / 1000
  if (!Number.isFinite(seconds) || seconds < 0) return ''
  return seconds < 60 ? `${seconds.toFixed(1).replace('.', ',')} s` : `${Math.floor(seconds / 60)} min ${String(Math.round(seconds % 60)).padStart(2, '0')} s`
}

function HelpSources({ sources }) {
  if (!sources?.length) return null
  return <div className="help-sources">
    <span>Sources</span>
    {sources.map((source, index) => {
      const content = <><b>{helpSourceKinds[source.kind] || source.kind}</b>{source.title}</>
      return source.url
        ? <a key={index} className={`help-source ${source.kind}`} href={source.url} target="_blank" rel="noreferrer noopener">{content}</a>
        : <span key={index} className={`help-source ${source.kind}`}>{content}</span>
    })}
  </div>
}

function HelpConsolePage({ token, user, tickets }) {
  const [options, setOptions] = useState(null)
  const [list, setList] = useState({ items: [], total_count: 0 })
  const [search, setSearch] = useState('')
  const [listPage, setListPage] = useState(1)
  const [listSize, setListSize] = useState(() => storedPageSize('help-conversations', 10))
  const [active, setActive] = useState(null)
  const [setup, setSetup] = useState(true)
  const [form, setForm] = useState({ company_id: '', ticket_id: '', use_kb_live: false, use_web: false, model: null })
  const [draft, setDraft] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const endRef = useRef(null)
  const aiOptions = options?.ai
  const pending = Boolean(active?.messages?.some((message) => message.status === 'pending'))
  const staffTickets = useMemo(() => (tickets || []).filter((item) => !['closed', 'rejected'].includes(item.status)), [tickets])

  useEffect(() => { request('/help/options', { token }).then(setOptions).catch((err) => setError(err.message)) }, [token])

  // La conversation ouverte survit à l'actualisation de la page.
  const activeKey = `oxe-help-active:${user.id}`
  const [restored, setRestored] = useState(false)
  useEffect(() => {
    let id = null
    try { id = localStorage.getItem(activeKey) } catch { /* stockage indisponible */ }
    if (!id) { setRestored(true); return }
    request(`/help/conversations/${id}`, { token })
      .then((conversation) => { setActive(conversation); setSetup(false) })
      .catch(() => { try { localStorage.removeItem(activeKey) } catch { /* ignoré */ } })
      .finally(() => setRestored(true))
  }, [activeKey, token])
  useEffect(() => {
    if (!restored) return
    try { if (active?.id) localStorage.setItem(activeKey, active.id); else localStorage.removeItem(activeKey) } catch { /* ignoré */ }
  }, [restored, active?.id, activeKey])

  const loadList = useCallback(async () => {
    try {
      const params = new URLSearchParams({ limit: String(listSize), offset: String((listPage - 1) * listSize) })
      if (search.trim()) params.set('q', search.trim())
      setList(await request(`/help/conversations?${params}`, { token }))
    } catch (err) {
      setError(err.message)
    }
  }, [token, listPage, listSize, search])

  useEffect(() => { loadList() }, [loadList])
  useEffect(() => { setListPage(1) }, [search, listSize])

  const refreshActive = useCallback(async (id) => {
    try {
      setActive(await request(`/help/conversations/${id}`, { token }))
    } catch (err) {
      setError(err.message)
    }
  }, [token])

  useEffect(() => {
    if (!pending || !active) return undefined
    const timer = window.setInterval(() => { refreshActive(active.id); loadList() }, 2500)
    return () => window.clearInterval(timer)
  }, [pending, active?.id, refreshActive, loadList])

  useEffect(() => { endRef.current?.scrollIntoView({ block: 'end' }) }, [active?.messages?.length, pending])

  async function run(action) {
    setBusy(true); setError('')
    try { await action() } catch (err) { setError(err.message) } finally { setBusy(false) }
  }

  const open = (id) => run(async () => { setActive(await request(`/help/conversations/${id}`, { token })); setSetup(false) })

  const start = (event) => {
    event.preventDefault()
    return run(async () => {
      const body = {
        use_kb_live: form.use_kb_live, use_web: form.use_web,
        ...(form.ticket_id ? { ticket_id: form.ticket_id } : form.company_id ? { company_id: form.company_id } : {}),
        ...(form.model ? { model: form.model } : {}),
      }
      const created = await request('/help/conversations', { token, method: 'POST', body: JSON.stringify(body) })
      setActive(created); setSetup(false); await loadList()
    })
  }

  const send = (event) => {
    event?.preventDefault()
    const content = draft.trim()
    if (!content || pending || !active) return undefined
    return run(async () => {
      setDraft('')
      setActive(await request(`/help/conversations/${active.id}/messages`, { token, method: 'POST', body: JSON.stringify({ content }) }))
      await loadList()
    })
  }

  const attach = (event) => {
    const chosen = Array.from(event.target.files || [])
    event.target.value = ''
    if (!chosen.length || !active) return undefined
    return run(async () => {
      const body = new FormData()
      chosen.forEach((file) => body.append('files', file))
      setActive(await request(`/help/conversations/${active.id}/files`, { token, method: 'POST', body }))
    })
  }

  const detach = (file) => run(async () => {
    setActive(await request(`/help/conversations/${active.id}/files/${file.id}`, { token, method: 'DELETE' }))
  })

  const patch = (changes) => run(async () => {
    setActive(await request(`/help/conversations/${active.id}`, { token, method: 'PATCH', body: JSON.stringify(changes) }))
  })

  const remove = (item) => window.confirm(`Supprimer la conversation « ${item.title} » ?`) && run(async () => {
    await request(`/help/conversations/${item.id}`, { token, method: 'DELETE' })
    if (active?.id === item.id) { setActive(null); setSetup(true) }
    await loadList()
  })

  const selectedTicket = staffTickets.find((item) => item.id === form.ticket_id)

  return <div className="help-page">
    <div className="welcome-row">
      <div><span className="eyebrow">ASSISTANT TECHNIQUE</span><h1>Console d’aide</h1><p>Posez vos questions techniques à l’assistant, indépendamment d’un ticket. Il s’appuie sur la documentation Alcatel, la KB constructeur et, si vous choisissez une entreprise, sur les documents privés de cette seule entreprise. Vos conversations restent personnelles.</p></div>
    </div>
    {error && <div className="alert error page-alert"><AlertCircle size={17} />{error}<button onClick={() => setError('')}>Fermer</button></div>}
    <div className="help-layout">
      <aside className="panel help-sidebar">
        <button className="button primary" onClick={() => { setActive(null); setSetup(true) }}><Plus size={16} />Nouvelle conversation</button>
        <input type="search" aria-label="Rechercher une conversation" placeholder="Rechercher…" value={search} onChange={(event) => setSearch(event.target.value)} />
        <div className="help-conversations">
          {list.items.map((item) => <div key={item.id} className={`help-conversation ${active?.id === item.id ? 'active' : ''}`}>
            <button type="button" onClick={() => open(item.id)}>
              <strong>{item.title}</strong>
              <span>{[item.ticket_reference, item.company_name || 'Sans entreprise'].filter(Boolean).join(' · ')} · {date(item.updated_at)}</span>
            </button>
            <button type="button" className="icon-button" aria-label={`Supprimer ${item.title}`} title="Supprimer" onClick={() => remove(item)}><Trash2 size={14} /></button>
          </div>)}
          {list.items.length === 0 && <div className="empty-state compact">Aucune conversation.</div>}
        </div>
        <Pagination page={listPage} pageSize={listSize} total={list.total_count} setPage={setListPage} setPageSize={(size) => { setListSize(size); rememberPageSize('help-conversations', size) }} sizes={[10, 25, 50]} label="conversations" compact />
      </aside>

      <section className="panel help-main">
        {setup && !active && <form className="help-setup" onSubmit={start}>
          <div className="panel-head"><div><h2>Nouvelle conversation</h2><p>Choisissez le périmètre de cette conversation. Il ne pourra plus être mélangé avec une autre entreprise.</p></div><Sparkles size={19} className="muted-icon" /></div>
          <label>Travailler sur un ticket <span className="optional">— facultatif</span>
            <select value={form.ticket_id} onChange={(event) => setForm({ ...form, ticket_id: event.target.value })}>
              <option value="">Aucun : conversation libre</option>
              {staffTickets.map((item) => <option key={item.id} value={item.id}>{item.reference} — {item.title}</option>)}
            </select>
            <span className="field-hint">Le contexte du ticket est copié dans la conversation (le ticket n’est jamais modifié) et son entreprise est appliquée.</span>
          </label>
          {options?.independent ? <p className="field-hint">Espace : <strong>{options.companies[0]?.name}</strong>. Vos documents d’entreprise (menu « Documents entreprise ») sont utilisés automatiquement.</p> : <label>Entreprise
            <select value={selectedTicket ? '' : form.company_id} disabled={Boolean(form.ticket_id)} onChange={(event) => setForm({ ...form, company_id: event.target.value })}>
              <option value="">{form.ticket_id ? 'Entreprise du ticket' : 'Aucune : connaissances communes uniquement'}</option>
              {(options?.companies || []).map((company) => <option key={company.id} value={company.id}>{company.name}</option>)}
            </select>
            <span className="field-hint">{user.role === 'agent' ? 'Vous ne voyez que vos entreprises affectées. ' : ''}Les documents privés de l’entreprise choisie s’ajoutent aux connaissances communes.</span>
          </label>}
          <fieldset className="help-toggles">
            <legend>Sources complémentaires</legend>
            <label className="ai-option-check"><input type="checkbox" disabled={!options?.kb_live} checked={form.use_kb_live} onChange={(event) => setForm({ ...form, use_kb_live: event.target.checked })} />Chercher en direct sur le portail constructeur{!options?.kb_live && <small> — indisponible</small>}</label>
            <label className="ai-option-check"><input type="checkbox" disabled={!options?.web_search} checked={form.use_web} onChange={(event) => setForm({ ...form, use_web: event.target.checked })} />Recherche web libre{!options?.web_search ? <small> — non configurée</small> : <small> — seule votre question, sans données personnelles, est envoyée</small>}</label>
          </fieldset>
          <AIModelPicker options={aiOptions} value={form.model} onChange={(model) => setForm({ ...form, model })} label="Modèle IA" />
          <div className="rag-form-footer"><span /><button className="button primary" disabled={busy}><MessageCircle size={16} />{busy ? 'Création…' : 'Démarrer'}</button></div>
        </form>}

        {active && <div className="help-chat">
          <div className="help-chat-head">
            <div><h2>{active.title}</h2><p>{[active.ticket_reference && `Ticket ${active.ticket_reference}`, active.company_name || 'Connaissances communes'].filter(Boolean).join(' · ')}</p></div>
            <div className="help-chat-tools">
              <label className="ai-option-check"><input type="checkbox" disabled={busy || pending || !options?.kb_live} checked={active.use_kb_live} onChange={(event) => patch({ use_kb_live: event.target.checked })} />Portail constructeur</label>
              <label className="ai-option-check"><input type="checkbox" disabled={busy || pending || !options?.web_search} checked={active.use_web} onChange={(event) => patch({ use_web: event.target.checked })} />Web</label>
              {active.ticket_id && <button type="button" className="button secondary" disabled={busy || pending} title="Recopier l’état actuel du ticket dans la conversation" onClick={() => patch({ refresh_ticket: true })}><RefreshCw size={15} />Actualiser le ticket</button>}
            </div>
          </div>
          <AIModelPicker options={aiOptions} value={active.model} onChange={(model) => patch({ model })} disabled={busy || pending} label="Modèle IA" />
          <div className="help-messages" aria-live="polite">
            {active.messages.length === 0 && <div className="empty-state compact">Posez votre première question ci-dessous.</div>}
            {active.messages.map((message) => <article key={message.id} className={`help-message ${message.role} ${message.status}`}>
              <div className="help-message-who">{message.role === 'user' ? <Avatar name={user.full_name} size="small" /> : <span className="avatar small assistant"><Sparkles size={13} /></span>}<b>{message.role === 'user' ? 'Vous' : 'Assistant'}</b>{message.model && <small>{message.model}</small>}<time className="help-time" dateTime={message.created_at} title={date(message.created_at)}>{helpTime(message.created_at)}</time>{message.role === 'assistant' && helpDuration(message) && <small className="help-duration" title="Temps de réponse de l’IA">⏱ {helpDuration(message)}</small>}</div>
              {message.thinking && <details className="help-thinking"><summary>Raisonnement de l’IA</summary><pre>{message.thinking}</pre></details>}
              {message.status === 'pending' && <p className="help-pending"><LoaderCircle size={15} className="ai-thinking-spinner" />L’assistant réfléchit…</p>}
              {message.status === 'error' && <div className="alert error"><AlertCircle size={16} />{message.error || 'Réponse impossible.'}</div>}
              {message.status === 'done' && <MessageBody body={message.content} isMarkdown />}
              <HelpSources sources={message.sources} />
            </article>)}
            <div ref={endRef} />
          </div>
          {active.files?.length > 0 && <div className="help-files" aria-label="Fichiers du contexte">
            {active.files.map((file) => <span key={file.id} className="help-file"><FileText size={13} />{file.filename}<small>{file.origin === 'ticket' ? 'du ticket · ' : ''}{formatBytes(file.size)}</small><button type="button" aria-label={`Retirer ${file.filename}`} title="Retirer du contexte" disabled={busy || pending} onClick={() => detach(file)}>×</button></span>)}
          </div>}
          <form className="help-composer" onSubmit={send}>
            <label className="button secondary help-attach" title="Ajouter des fichiers au contexte (PDF, Word, PowerPoint, Excel, texte, configurations…)"><Paperclip size={16} />Fichier<input type="file" multiple hidden disabled={busy || pending} onChange={attach} /></label>
            <textarea rows={3} maxLength={4000} placeholder="Votre question… (Entrée pour envoyer, Maj+Entrée pour un retour à la ligne)" value={draft} disabled={pending}
              onChange={(event) => setDraft(event.target.value)}
              onKeyDown={(event) => { if (event.key === 'Enter' && !event.shiftKey) send(event) }} />
            <button className="button primary" disabled={busy || pending || !draft.trim()}><Send size={16} />Envoyer</button>
          </form>
        </div>}
      </section>
    </div>
  </div>
}

const companyDocStatuses = {
  pending: ['Indexation…', 'blue'],
  indexed: ['Indexé', 'green'],
  empty: ['Aucun texte', 'amber'],
  failed: ['Échec', 'red'],
}

function CompanyDocsPage({ token, user, companies }) {
  const isCustomer = user.role === 'customer'
  const [companyId, setCompanyId] = useState('')
  const [data, setData] = useState(null)
  const [files, setFiles] = useState([])
  const [description, setDescription] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [skipped, setSkipped] = useState([])
  const documents = data?.documents || []
  const pager = usePagination(documents, { storageKey: 'company-docs', initialSize: 10 })

  useEffect(() => {
    if (!isCustomer && !companyId && companies?.length) setCompanyId(companies[0].id)
  }, [isCustomer, companyId, companies])

  const load = useCallback(async () => {
    if (!isCustomer && !companyId) return
    try {
      setData(await request(`/company-docs${isCustomer ? '' : `?company_id=${companyId}`}`, { token }))
      setError('')
    } catch (err) {
      setError(err.message)
    }
  }, [token, isCustomer, companyId])

  useEffect(() => { load() }, [load])
  useEffect(() => {
    if (!documents.some((doc) => doc.status === 'pending')) return undefined
    const timer = window.setInterval(load, 4000)
    return () => window.clearInterval(timer)
  }, [documents, load])

  async function upload(event) {
    event.preventDefault()
    if (!files.length) return
    const form = event.currentTarget
    const body = new FormData()
    files.forEach((file) => body.append('files', file))
    if (!isCustomer) body.append('company_id', companyId)
    if (description.trim()) body.append('description', description.trim())
    setBusy(true); setError(''); setNotice(''); setSkipped([])
    try {
      const result = await request('/company-docs', { token, method: 'POST', body })
      setFiles([]); setDescription(''); form.reset()
      setSkipped(result.skipped)
      setNotice(result.uploaded.length ? `${result.uploaded.length} document${result.uploaded.length > 1 ? 's' : ''} ajouté${result.uploaded.length > 1 ? 's' : ''} : indexation en cours.` : '')
      await load()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  async function act(path, options, message) {
    setBusy(true); setError(''); setNotice('')
    try {
      await request(path, { token, ...options })
      if (message) setNotice(message)
      await load()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  const remove = (doc) => window.confirm(`Supprimer « ${doc.filename} » ?\n\nLe fichier et ses extraits indexés seront supprimés définitivement.`)
    && act(`/company-docs/${doc.id}`, { method: 'DELETE' }, 'Document supprimé.')
  const download = (doc) => downloadPdf(`/company-docs/${doc.id}/download`, token, doc.filename).catch((err) => setError(err.message))
  const used = data?.used_bytes ?? 0
  const quota = data?.max_bytes || 1
  const company = data?.company?.name || user.company_name

  return <div className="company-docs-page">
    <div className="welcome-row">
      <div><span className="eyebrow">CONNAISSANCES DE L’ENTREPRISE</span><h1>Documents de {company || 'l’entreprise'}</h1><p>Architecture réseau, plan d’adressage, matrice de flux… Ces documents sont <strong>privés</strong> : seuls les membres de l’entreprise et les administrateurs peuvent les ajouter. L’assistant IA s’en sert pour diagnostiquer vos incidents, en complément de la documentation Alcatel, sans jamais les partager avec une autre entreprise.</p></div>
      <button className="button secondary" onClick={load}><RefreshCw size={16} />Actualiser</button>
    </div>
    {!isCustomer && <div className="company-docs-picker"><label>Entreprise<select value={companyId} onChange={(event) => { setCompanyId(event.target.value); setData(null) }}>{companies.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label>{user.role === 'agent' && <span className="field-hint">Lecture seule : les documents sont ajoutés par l’entreprise ou par un administrateur.</span>}</div>}
    {error && <div className="alert error page-alert"><AlertCircle size={17} />{error}</div>}
    {notice && <div className="alert success page-alert"><Check size={16} />{notice}</div>}
    {skipped.length > 0 && <div className="alert warning page-alert company-docs-skipped"><AlertCircle size={17} /><div><strong>{skipped.length} fichier{skipped.length > 1 ? 's' : ''} non ajouté{skipped.length > 1 ? 's' : ''}</strong><ul>{skipped.map((item, index) => <li key={index}><b>{item.filename}</b> — {item.reason}</li>)}</ul></div></div>}

    {data?.can_edit && <section className="panel">
      <div className="panel-head"><div><h2>Ajouter des documents</h2><p>Jusqu’à {data.max_files} fichiers de {Math.round(data.max_file_bytes / 1048576)} Mo : {data.formats.join(' ')}. Les doublons sont détectés automatiquement.</p></div><FilePlus2 size={19} className="muted-icon" /></div>
      <form className="company-docs-upload" onSubmit={upload}>
        <label>Fichiers<input type="file" multiple accept={data.formats.join(',')} onChange={(event) => setFiles(Array.from(event.target.files || []))} /></label>
        <label>Description <span className="optional">— facultatif</span><input maxLength={500} value={description} onChange={(event) => setDescription(event.target.value)} placeholder="Ex. Plan d’adressage du site de Lyon, version 2026" /></label>
        <div className="rag-form-footer"><span>{files.length ? files.map((file) => file.name).join(', ') : 'Aucun fichier sélectionné.'}</span><button className="button primary" disabled={busy || files.length === 0 || files.length > data.max_files}><FilePlus2 size={16} />{busy ? 'Envoi…' : 'Ajouter'}</button></div>
      </form>
    </section>}

    <section className="panel">
      <div className="panel-head"><div><h2>Documents de l’entreprise</h2><p>{documents.length} document{documents.length > 1 ? 's' : ''} · {formatBytes(used)} sur {formatBytes(data?.max_bytes)} utilisés</p></div><FileText size={19} className="muted-icon" /></div>
      <div className="company-docs-quota" aria-hidden="true"><span style={{ width: `${Math.min(100, (used / quota) * 100)}%` }} /></div>
      {documents.length ? <div className="company-docs-list">
        {pager.items.map((doc) => <div className="company-doc-row" key={doc.id}>
          <FileText size={18} />
          <div className="company-doc-main"><strong>{doc.filename}</strong><span>{formatBytes(doc.size)} · ajouté le {date(doc.created_at)}{doc.chunk_count ? ` · ${doc.chunk_count} extrait${doc.chunk_count > 1 ? 's' : ''}` : ''}{doc.description ? ` · ${doc.description}` : ''}</span>{doc.error && <small className="company-doc-error">{doc.error}</small>}</div>
          <Badge value={(companyDocStatuses[doc.status] || [doc.status])[0]} map={{ [(companyDocStatuses[doc.status] || [doc.status])[0]]: companyDocStatuses[doc.status] || [doc.status, 'gray'] }} />
          <div className="company-doc-actions">
            <button type="button" className="icon-button" title="Télécharger l’original" aria-label={`Télécharger ${doc.filename}`} onClick={() => download(doc)}><ArrowDownLeft size={15} /></button>
            {data.can_edit && doc.status === 'failed' && <button type="button" className="icon-button" title="Relancer l’indexation" aria-label={`Réindexer ${doc.filename}`} disabled={busy} onClick={() => act(`/company-docs/${doc.id}/reindex`, { method: 'POST' }, 'Indexation relancée.')}><RefreshCw size={15} /></button>}
            {data.can_edit && <button type="button" className="icon-button rag-delete-document" title="Supprimer" aria-label={`Supprimer ${doc.filename}`} disabled={busy} onClick={() => remove(doc)}><Trash2 size={15} /></button>}
          </div>
        </div>)}
      </div> : <div className="empty-state compact">{data ? 'Aucun document pour le moment.' : 'Chargement…'}</div>}
      <Pagination {...pager} sizes={[10, 25, 50]} label="documents" />
    </section>
  </div>
}

function RagAdminPage({ token }) {
  const [overview, setOverview] = useState(null)
  const [files, setFiles] = useState([])
  const [query, setQuery] = useState('')
  const [results, setResults] = useState(null)
  const [busy, setBusy] = useState(false)
  const [searchBusy, setSearchBusy] = useState(false)
  const [selected, setSelected] = useState([])
  const docPager = usePagination(overview?.documents || [], { storageKey: 'rag-docs', initialSize: 25 })
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')

  const loadOverview = useCallback(async () => {
    try {
      setOverview(await request('/admin/rag', { token }))
      setError('')
    } catch (err) {
      setError(err.message)
    }
  }, [token])

  useEffect(() => { loadOverview() }, [loadOverview])
  useEffect(() => {
    if (!['pending', 'running'].includes(overview?.indexing?.status)) return undefined
    const refreshProgress = async () => {
      try {
        const indexing = await request('/admin/rag/index', { token })
        setOverview((current) => current ? { ...current, indexing } : current)
        if (['complete', 'error'].includes(indexing.status)) await loadOverview()
      } catch (err) {
        setError(err.message)
      }
    }
    const timer = window.setInterval(refreshProgress, 1500)
    return () => window.clearInterval(timer)
  }, [loadOverview, token, overview?.indexing?.status])

  async function uploadDocuments(event) {
    event.preventDefault()
    if (!files.length) return
    const form = event.currentTarget
    setBusy(true)
    setError('')
    setNotice('')
    const body = new FormData()
    files.forEach((file) => body.append('files', file))
    try {
      const uploaded = await request('/admin/rag/documents', { token, method: 'POST', body })
      setFiles([])
      form.reset()
      setNotice(`${uploaded.uploaded.length} document${uploaded.uploaded.length === 1 ? '' : 's'} ajouté${uploaded.uploaded.length === 1 ? '' : 's'}. Lancez la synchronisation pour les indexer.`)
      await loadOverview()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  async function changeEmbeddingModel(option) {
    if (!window.confirm(`Passer au modèle ${option.label} (${option.dimension} dimensions) ?\n\nLe corpus sera réindexé dans une collection dédiée à ce modèle; l’index du modèle précédent est conservé. La recherche est limitée tant que l’indexation n’est pas terminée.`)) return
    setBusy(true)
    setError('')
    setNotice('')
    try {
      await request('/admin/rag/embedding-model', { token, method: 'PUT', body: JSON.stringify({ model: option.id }) })
      setNotice(`Modèle d’embedding changé pour ${option.label}. Réindexation du corpus lancée.`)
      await loadOverview()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  async function deleteDocuments(names) {
    if (!names.length) return
    const label = names.length === 1 ? `le document « ${names[0]} »` : `${names.length} documents`
    if (!window.confirm(`Supprimer ${label} du corpus ?\n\nLes fichiers sources et leurs extraits indexés seront supprimés définitivement.`)) return
    setBusy(true)
    setError('')
    setNotice('')
    try {
      const result = await request('/admin/rag/documents', { token, method: 'DELETE', body: JSON.stringify({ names }) })
      setSelected((current) => current.filter((name) => !names.includes(name)))
      setNotice(`${result.deleted.length} document${result.deleted.length === 1 ? '' : 's'} supprimé${result.deleted.length === 1 ? '' : 's'}.${result.index_updated ? '' : ' L’index sera nettoyé à la prochaine synchronisation.'}`)
      await loadOverview()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  async function startIndexing() {
    setBusy(true)
    setError('')
    setNotice('')
    try {
      await request('/admin/rag/index', { token, method: 'POST' })
      setNotice('Synchronisation du corpus lancée.')
      await loadOverview()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  async function testSearch(event) {
    event.preventDefault()
    setSearchBusy(true)
    setError('')
    setResults(null)
    try {
      setResults(await request('/admin/rag/search', {
        token,
        method: 'POST',
        body: JSON.stringify({ query }),
      }))
    } catch (err) {
      setError(err.message)
    } finally {
      setSearchBusy(false)
    }
  }

  const metrics = overview?.metrics
  const indexingStatus = overview?.indexing?.status
  const uploadLimit = overview?.limits?.max_file_size ?? 52428800
  const uploadCountLimit = overview?.limits?.max_files ?? 50
  const uploadLimitLabel = uploadLimit >= 1048576
    ? `${(uploadLimit / 1048576).toFixed(uploadLimit % 1048576 === 0 ? 0 : 1)} Mio`
    : uploadLimit >= 1024 ? `${Math.round(uploadLimit / 1024)} Kio` : `${uploadLimit} octets`
  const statusLabels = {
    indexed: 'Indexé',
    pending: 'À indexer',
    modified: 'Modifié',
    empty: 'Sans texte',
  }

  return <div className="rag-admin-page">
    <div className="welcome-row">
      <div><span className="eyebrow">ADMINISTRATION IA</span><h1>Corpus documentaire RAG</h1><p>Surveillez les services, gérez les documents et contrôlez les résultats de recherche utilisés par l’assistance IA.</p></div>
      <button className="button secondary" onClick={loadOverview}><RefreshCw size={16} />Actualiser</button>
    </div>
    {error && <div className="alert error page-alert"><AlertCircle size={17} />{error}</div>}
    {notice && <div className="alert success page-alert"><Check size={16} />{notice}</div>}

    <section className="rag-service-grid">
      {[
        ['Qdrant', overview?.qdrant, overview?.collection],
        ['Embeddings', overview?.embedding, overview?.embedding_model],
      ].map(([label, service, detail]) => (
        <article className="panel rag-service" key={label}>
          <div className="rag-service-title"><Database size={18} /><strong>{label}</strong><Badge value={service?.status || 'unknown'} map={{ healthy: ['Disponible', 'green'], warning: ['À vérifier', 'amber'], down: ['Indisponible', 'red'], unknown: ['Chargement', 'gray'] }} /></div>
          <span>{detail || '—'}{label === 'Qdrant' && overview?.embedding_dimension ? ` · ${overview.embedding_dimension} dimensions` : ''}</span>
          {service?.error && <small>{service.error}</small>}
        </article>
      ))}
    </section>

    <section className="panel rag-panel rag-embedding-panel">
      <div className="panel-head"><div><h2>Modèle d’embedding</h2><p>Chaque modèle a sa propre collection Qdrant. Changer de modèle réindexe le corpus; seuls les modèles téléchargés sur l’instance d’embeddings sont sélectionnables.</p></div><Database size={19} className="muted-icon" /></div>
      <div className="rag-embedding-options">
        {(overview?.embedding_options || []).map((option) => <label key={option.id} className={`rag-embedding-option ${option.selected ? 'selected' : ''} ${option.selectable && option.installed ? '' : 'unavailable'}`}>
          <input type="radio" name="embedding-model" checked={option.selected} disabled={busy || !option.selectable || !option.installed || ['pending', 'running'].includes(indexingStatus)} onChange={() => !option.selected && changeEmbeddingModel(option)} />
          <div><strong>{option.label}</strong><span>{option.id} · {option.dimension} dimensions</span></div>
          {option.selected ? <Badge value="Actif" map={{ Actif: ['Actif', 'green'] }} /> : !option.installed && <Badge value="Non téléchargé" map={{ 'Non téléchargé': ['Non téléchargé', 'amber'] }} />}
        </label>)}
      </div>
    </section>

    <section className="stat-grid rag-stat-grid">
      <StatCard icon={<FileText size={18} />} label="Documents" value={metrics?.source_count ?? '—'} hint={`${metrics?.indexed_sources ?? 0} indexés`} tone="blue" />
      <StatCard icon={<Activity size={18} />} label="À synchroniser" value={(metrics?.pending_sources ?? 0) + (metrics?.deleted_sources ?? 0)} hint={`${metrics?.deleted_sources ?? 0} source${metrics?.deleted_sources === 1 ? '' : 's'} supprimée${metrics?.deleted_sources === 1 ? '' : 's'}`} tone="red" />
      <StatCard icon={<Database size={18} />} label="Extraits indexés" value={metrics?.chunk_count ?? '—'} hint={`${metrics?.empty_sources ?? 0} document${metrics?.empty_sources === 1 ? '' : 's'} sans texte extractible`} tone="green" />
    </section>

    <div className="rag-action-grid">
      <section className="panel rag-panel">
        <div className="panel-head"><div><h2>Ajouter des documents</h2><p>PDF textuels, Markdown ou fichiers texte; {uploadLimitLabel} maximum par fichier.</p></div><FilePlus2 size={19} className="muted-icon" /></div>
        <form className="rag-upload-form" onSubmit={uploadDocuments}>
          <label>Documents à ajouter<input type="file" accept=".pdf,.md,.markdown,.txt" multiple onChange={(event) => setFiles(Array.from(event.target.files || []))} /></label>
          <div className="rag-form-footer"><span>{files.length ? files.map((file) => file.name).join(', ') : `Maximum ${uploadCountLimit} documents par envoi.`}</span><button className="button primary" disabled={busy || files.length === 0}><FilePlus2 size={16} />{busy ? 'Envoi…' : 'Ajouter'}</button></div>
        </form>
      </section>
      <section className="panel rag-panel">
        <div className="panel-head"><div><h2>Synchroniser l’index</h2><p>Ajoute ou met à jour les sources et retire de Qdrant celles supprimées.</p></div><RefreshCw size={19} className="muted-icon" /></div>
        <div className="rag-index-action">
          <div><strong>{indexingStatus === 'running' || indexingStatus === 'pending' ? 'Synchronisation en cours' : indexingStatus === 'error' ? 'Dernière synchronisation en erreur' : indexingStatus === 'complete' ? 'Dernière synchronisation terminée' : 'Prêt à synchroniser'}</strong>
            {overview?.indexing?.error && <span className="rag-error-detail">{overview.indexing.error}</span>}
            {overview?.indexing?.result && <span>{overview.indexing.result.indexed_sources} ajouté(s)/mis à jour · {overview.indexing.result.deleted_sources} supprimé(s)</span>}
          </div>
          <button className="button primary" disabled={busy || ['pending', 'running'].includes(indexingStatus)} onClick={startIndexing}><RefreshCw size={16} />Lancer</button>
        </div>
        {['pending', 'running'].includes(indexingStatus) && <div className="rag-index-progress" role="status" aria-live="polite">
          <LoaderCircle size={19} className="rag-spinner" />
          <div className="rag-progress-content">
            <strong>{overview?.indexing?.progress?.message || 'Indexation en cours…'}</strong>
            {overview?.indexing?.progress?.total > 0 && <>
              <div className="rag-progress-track"><span style={{ width: `${Math.min(100, Math.round(overview.indexing.progress.processed / overview.indexing.progress.total * 100))}%` }} /></div>
              <span>{overview.indexing.progress.processed} / {overview.indexing.progress.total} document{overview.indexing.progress.total > 1 ? 's' : ''}</span>
            </>}
            {overview?.indexing?.progress?.current && <span>{overview.indexing.progress.current}</span>}
          </div>
        </div>}
        {indexingStatus === 'complete' && <div className="rag-index-complete"><Check size={16} />Indexation terminée.</div>}
      </section>
    </div>

    <section className="panel rag-panel rag-document-panel">
      <div className="panel-head"><div><h2>Documents du corpus</h2><p>État des fichiers sources détectés dans le dossier documentaire.</p></div><FileText size={19} className="muted-icon" /></div>
      {overview?.documents?.length > 0 && <div className="rag-document-toolbar">
        <label><input type="checkbox" checked={selected.length === overview.documents.length} onChange={(event) => setSelected(event.target.checked ? overview.documents.map((document) => document.source) : [])} />Tout sélectionner</label>
        <button className="button secondary danger" disabled={busy || selected.length === 0 || ['pending', 'running'].includes(indexingStatus)} onClick={() => deleteDocuments(selected)}><Trash2 size={15} />Supprimer la sélection{selected.length ? ` (${selected.length})` : ''}</button>
      </div>}
      {overview?.documents?.length ? <><div className="rag-document-list">
        {docPager.items.map((document) => <div className="rag-document-row" key={document.source}>
          <input type="checkbox" aria-label={`Sélectionner ${document.source}`} checked={selected.includes(document.source)} onChange={(event) => setSelected((current) => event.target.checked ? [...current, document.source] : current.filter((name) => name !== document.source))} />
          <FileText size={17} />
          <div><strong>{document.source}</strong><span>{(document.size_bytes / 1024).toFixed(1)} Ko · {document.chunks} extrait{document.chunks === 1 ? '' : 's'}</span></div>
          <Badge value={statusLabels[document.status] || document.status} map={{
            [statusLabels.indexed]: ['Indexé', 'green'],
            [statusLabels.pending]: ['À indexer', 'amber'],
            [statusLabels.modified]: ['Modifié', 'blue'],
            [statusLabels.empty]: ['Sans texte', 'red'],
          }} />
          <button className="icon-button rag-delete-document" title="Supprimer ce document" aria-label={`Supprimer ${document.source}`} disabled={busy || ['pending', 'running'].includes(indexingStatus)} onClick={() => deleteDocuments([document.source])}><Trash2 size={15} /></button>
        </div>)}
      </div><Pagination {...docPager} sizes={[10, 25, 50, 100]} label="documents" /></> : <div className="empty-state compact">{overview ? 'Aucun document compatible dans le corpus.' : 'Chargement des documents…'}</div>}
      {overview?.deleted_sources?.length > 0 && <div className="rag-deleted-sources"><strong>Sources Qdrant à supprimer à la prochaine synchronisation</strong><span>{overview.deleted_sources.join(', ')}</span></div>}
    </section>

    <section className="panel rag-panel rag-search-panel">
      <div className="panel-head"><div><h2>Évaluer une recherche</h2><p>Teste la recherche vectorielle sur l’index existant, sans modifier ni réindexer le corpus.</p></div><Search size={19} className="muted-icon" /></div>
      <form className="rag-query-form" onSubmit={testSearch}>
        <label>Question de test<textarea required maxLength={2000} rows={2} value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Ex. Comment rétablir les appels entrants ?" /></label>
        <button className="button primary" disabled={searchBusy || !query.trim()}><Search size={16} />{searchBusy ? 'Recherche…' : 'Tester la recherche'}</button>
      </form>
      {results && <div className="rag-results">
        <strong>{results.result_count} résultat{results.result_count === 1 ? '' : 's'} pour « {results.query} »</strong>
        {results.results.length ? results.results.map((result, index) => <article className="rag-result" key={`${result.source}-${result.page}-${index}`}>
          <div><strong>{result.source}{result.page ? ` · page ${result.page}` : ''}</strong><span>Score : {Number(result.score).toFixed(3)}</span></div>
          <p>{result.text}</p>
        </article>) : <p className="field-hint">Aucun extrait ne dépasse le seuil de pertinence configuré.</p>}
      </div>}
    </section>
  </div>
}


const kbJobStatuses = {
  success: ['Réussie', 'green'],
  failed: ['Échec', 'red'],
  skipped: ['Ignorée', 'amber'],
  running: ['En cours', 'blue'],
}
const kbResultStatuses = {
  new: ['Indexé', 'green'],
  known: ['Déjà connu', 'blue'],
  duplicate: ['Doublon évité', 'amber'],
  filtered: ['Écarté', 'gray'],
  empty: ['Aucun résultat', 'gray'],
  failed: ['Échec', 'red'],
}
const kbSources = { kb: 'Articles KB', document: 'Documents' }
const kbSessionStates = {
  valid: ['Session valide', 'green'],
  expired: ['Session expirée', 'red'],
  unknown: ['Non vérifiée', 'gray'],
}

function kbBadge(table, key) {
  const [label, color] = table[key] || [key, 'gray']
  return <Badge value={label} map={{ [label]: [label, color] }} />
}

function formatBytes(bytes) {
  if (bytes == null) return '—'
  if (bytes < 1024) return `${bytes} o`
  if (bytes < 1048576) return `${(bytes / 1024).toFixed(0)} Ko`
  if (bytes < 1073741824) return `${(bytes / 1048576).toFixed(1)} Mo`
  return `${(bytes / 1073741824).toFixed(2)} Go`
}

function relativeTime(value) {
  if (!value) return ''
  const seconds = Math.round((Date.now() - new Date(value).getTime()) / 1000)
  if (seconds < 45) return 'à l’instant'
  if (seconds < 3600) return `il y a ${Math.max(1, Math.round(seconds / 60))} min`
  if (seconds < 86400) return `il y a ${Math.round(seconds / 3600)} h`
  return date(value)
}

function KBResultList({ results }) {
  if (!results?.length) return <p className="field-hint">Aucun résultat enregistré pour cette recherche.</p>
  return <div className="kb-results">
    {Object.entries(kbSources).map(([source, label]) => {
      const items = results.filter((item) => item.source === source)
      if (!items.length) return null
      return <div className="kb-result-group" key={source}>
        <h4>{label} <span>{items.length}</span></h4>
        {items.map((item, index) => <div className="kb-result" key={`${item.url}-${index}`}>
          {kbBadge(kbResultStatuses, item.status)}
          <div>
            {item.url ? <a href={item.url} target="_blank" rel="noreferrer">{item.title || item.url}</a> : <strong>{item.title}</strong>}
            {item.detail && <small>{item.detail}</small>}
          </div>
        </div>)}
      </div>
    })}
  </div>
}

function KBJobFacts({ job }) {
  const bySource = (source) => (job.results || []).filter((item) => item.source === source).length
  return <div className="kb-job-facts">
    {bySource('kb') > 0 && <span className="kb-chip">KB · {bySource('kb')}</span>}
    {bySource('document') > 0 && <span className="kb-chip">Docs · {bySource('document')}</span>}
    {job.new_documents > 0 && <span className="kb-chip good">{job.new_documents} indexé{job.new_documents > 1 ? 's' : ''}</span>}
    {job.skipped_documents > 0 && <span className="kb-chip">{job.skipped_documents} déjà connu{job.skipped_documents > 1 ? 's' : ''}</span>}
    {job.duration_ms > 0 && <span className="kb-chip muted">{(job.duration_ms / 1000).toFixed(1)} s</span>}
    <time title={date(job.created_at)}>{relativeTime(job.created_at)}</time>
  </div>
}

function KBJobTitle({ job, onOpenTicket }) {
  if (job.kind === 'reindex') return <strong>Réindexation des articles</strong>
  if (!job.ticket_reference) return <strong>Ticket supprimé</strong>
  return <>
    <button type="button" className="kb-link" onClick={() => onOpenTicket?.(job.ticket_id)}>{job.ticket_reference}</button>
    <span className="kb-job-ticket">{job.ticket_title}</span>
  </>
}

function KBLogLine({ entry }) {
  return <li className={`kb-log-${entry.level}`}>
    <time>{entry.ts ? new Date(entry.ts).toLocaleTimeString('fr-FR') : ''}</time>
    <b>{entry.event}</b>
    {entry.query && <span>« {entry.query} »</span>}
    {entry.selector && <code>{entry.selector}</code>}
    {entry.url && <span className="kb-log-url">{entry.url}</span>}
    {entry.message && <span>{entry.message}</span>}
    {typeof entry.hits === 'number' && <span>{entry.hits} résultat{entry.hits > 1 ? 's' : ''}</span>}
  </li>
}

function KBAdminPage({ token, onOpenTicket }) {
  const [status, setStatus] = useState(null)
  const [debug, setDebug] = useState(null)
  const [showDebug, setShowDebug] = useState(false)
  const [selectedJobId, setSelectedJobId] = useState(null)
  const [debugTab, setDebugTab] = useState('search')
  const [filter, setFilter] = useState('all')
  const [jobsPage, setJobsPage] = useState(1)
  const [jobsPageSize, setJobsPageSizeState] = useState(() => storedPageSize('kb-jobs', 10))
  const [debugPage, setDebugPage] = useState(1)
  const [archive, setArchive] = useState(null)
  const [expanded, setExpanded] = useState([])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')

  const load = useCallback(async () => {
    try {
      const jobsQuery = new URLSearchParams({ jobs_limit: String(jobsPageSize), jobs_offset: String((jobsPage - 1) * jobsPageSize) })
      if (filter !== 'all') jobsQuery.set('jobs_status', filter)
      setStatus(await request(`/admin/kb?${jobsQuery}`, { token }))
      request('/admin/kb/archive', { token }).then(setArchive).catch(() => setArchive(null))
      if (showDebug) {
        const debugQuery = new URLSearchParams({ limit: '12', offset: String((debugPage - 1) * 12) })
        if (selectedJobId) debugQuery.set('job_id', selectedJobId)
        setDebug(await request(`/admin/kb/debug?${debugQuery}`, { token }))
      }
      setError('')
    } catch (err) {
      setError(err.message)
    }
  }, [token, showDebug, jobsPage, jobsPageSize, filter, debugPage, selectedJobId])

  useEffect(() => {
    load()
    const timer = window.setInterval(load, 10000)
    return () => window.clearInterval(timer)
  }, [load])

  useEffect(() => {
    if (debug?.jobs?.length && !selectedJobId) setSelectedJobId(debug.jobs[0].id)
  }, [debug, selectedJobId])

  async function act(path, options, successMessage) {
    setBusy(true)
    setError('')
    setNotice('')
    try {
      await request(path, { token, ...options })
      if (successMessage) setNotice(successMessage)
      await load()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  function debugJob(jobId) {
    setSelectedJobId(jobId)
    setDebugTab('search')
    setShowDebug(true)
    window.setTimeout(() => document.getElementById('kb-debug')?.scrollIntoView({ behavior: 'smooth', block: 'start' }), 150)
  }

  const metrics = status?.metrics
  const session = status?.session
  const sessionBadge = kbSessionStates[session?.state] || kbSessionStates.unknown
  const duration = metrics?.average_duration_ms != null ? `${(metrics.average_duration_ms / 1000).toFixed(1)} s` : '—'
  const jobs = status?.recent_jobs || []
  const counts = status?.job_counts || { all: 0, success: 0, failed: 0, skipped: 0 }
  const visibleJobs = jobs
  const selectedJob = debug?.jobs?.find((job) => job.id === selectedJobId) || (debug?.focused_job?.id === selectedJobId ? debug.focused_job : null)
  const logPager = usePagination(debug?.browser_logs || [], { storageKey: 'kb-logs', initialSize: 50 })
  const jobLogs = (() => {
    if (!selectedJob || !debug?.browser_logs) return []
    const from = new Date(selectedJob.created_at).getTime() - 3000
    const to = (selectedJob.finished_at ? new Date(selectedJob.finished_at).getTime() : Date.now()) + 6000
    return [...debug.browser_logs]
      .filter((entry) => { const at = new Date(entry.ts).getTime(); return at >= from && at <= to })
      .sort((a, b) => new Date(a.ts) - new Date(b.ts))
  })()

  return <div className="kb-admin-page">
    <div className="welcome-row">
      <div><span className="eyebrow">ADMINISTRATION IA</span><h1>Base de connaissances constructeur</h1><p>Enrichit le RAG avec les articles du Technical Knowledge Center et les documents (guides, présentations, packs) de la KB Alcatel via un navigateur conteneurisé. Si le module est coupé ou en panne, l’assistance IA fonctionne exactement comme avant.</p></div>
      <button className="button secondary" onClick={load}><RefreshCw size={16} />Actualiser</button>
    </div>
    {error && <div className="alert error page-alert"><AlertCircle size={17} />{error}</div>}
    {notice && <div className="alert success page-alert"><Check size={16} />{notice}</div>}
    {status && !status.env_enabled && <div className="alert warning page-alert"><AlertCircle size={17} />Module désactivé par la configuration serveur (ENABLE_KB_BROWSER_SCRAPER=false). Activez-le dans le .env puis redémarrez l’API; le navigateur se lance avec <code>docker compose --profile kb up -d</code>.</div>}
    {session?.last_alert && <div className="alert error page-alert"><AlertCircle size={17} />{session.last_alert}{session.last_alert_at ? ` (${date(session.last_alert_at)})` : ''}</div>}

    <section className="panel kb-control-panel">
      <div className="panel-head"><div><h2>Statut et contrôle</h2><p>Interrupteur en direct, état du navigateur et de la session (MFA).</p></div><Database size={19} className="muted-icon" /></div>
      <div className="kb-control-grid">
        <label className="ai-toggle"><input type="checkbox" checked={Boolean(status?.enabled) && Boolean(status?.env_enabled)} disabled={busy || !status?.env_enabled} onChange={(event) => act('/admin/kb/settings', { method: 'PUT', body: JSON.stringify({ enabled: event.target.checked }) }, event.target.checked ? 'Enrichissement KB activé.' : 'Enrichissement KB désactivé.')} /><span>{status?.effective ? 'Enrichissement actif' : 'Enrichissement inactif'}</span></label>
        <div className="kb-state"><span>Navigateur</span><Badge value={status?.browser_online ? 'En ligne' : 'Hors ligne'} map={{ 'En ligne': ['En ligne', 'green'], 'Hors ligne': ['Hors ligne', 'red'] }} /></div>
        <div className="kb-state"><span>Session KB</span><Badge value={sessionBadge[0]} map={{ [sessionBadge[0]]: sessionBadge }} />{session?.checked_at && <small>vérifiée le {date(session.checked_at)}</small>}</div>
        <div className="kb-state"><span>File d’attente</span><strong>{status?.queue_size ?? 0}</strong></div>
      </div>
      <div className="kb-actions">
        <button className="button secondary" disabled={busy || !status?.env_enabled} onClick={() => act('/admin/kb/session/check', { method: 'POST' }, 'Session vérifiée.')}><ShieldCheck size={15} />Vérifier la session</button>
        {status?.login_url && <a className="button secondary" href={status.login_url} target="_blank" rel="noreferrer"><ArrowUpRight size={15} />Se reconnecter (MFA)</a>}
        <button className="button secondary" disabled={busy || !status?.env_enabled} onClick={() => act('/admin/kb/reindex', { method: 'POST' }, 'Réindexation des éléments KB planifiée.')}><RefreshCw size={15} />Réindexer les éléments</button>
      </div>
    </section>

    <section className="stat-grid kb-stat-grid">
      <StatCard icon={<Search size={18} />} label="Recherches KB" value={metrics?.searches ?? '—'} hint={`${metrics?.skipped ?? 0} ignorée${metrics?.skipped === 1 ? '' : 's'} (module coupé, hors ligne…)`} tone="blue" />
      <StatCard icon={<CheckCheck size={18} />} label="Taux de succès" value={metrics?.success_rate != null ? `${metrics.success_rate} %` : '—'} hint={`${metrics?.success ?? 0} réussie${metrics?.success === 1 ? '' : 's'} · ${metrics?.failed ?? 0} échec${metrics?.failed === 1 ? '' : 's'}`} tone="green" />
      <StatCard icon={<Clock3 size={18} />} label="Durée moyenne" value={duration} hint="Par recherche (articles + documents)" tone="amber" />
      <StatCard icon={<FilePlus2 size={18} />} label="Éléments indexés" value={metrics?.documents_indexed ?? '—'} hint={`${metrics?.known_by_source?.kb ?? 0} article${(metrics?.known_by_source?.kb ?? 0) === 1 ? '' : 's'} · ${metrics?.known_by_source?.document ?? 0} document${(metrics?.known_by_source?.document ?? 0) === 1 ? '' : 's'} en base`} tone="red" />
    </section>

    <section className="panel kb-archive-panel">
      <div className="panel-head"><div><h2>Archive locale</h2><p>Copie des éléments récupérés (fichiers originaux et texte) dans le dossier <code>kb_archive/</code> de votre projet, hors Docker : elle survit à un « docker compose down -v » et permet de reconstruire la base sans retourner sur le portail.</p></div><HardDrive size={19} className="muted-icon" /></div>
      {archive?.enabled === false ? <p className="field-hint kb-archive-note">Archive désactivée (KB_ARCHIVE_ENABLED=false).</p> : <>
        <div className="kb-archive-stats">
          <div><strong>{archive?.items ?? '—'}</strong><span>élément{archive?.items === 1 ? '' : 's'} (texte)</span></div>
          <div><strong>{archive?.files ?? '—'}</strong><span>fichier{archive?.files === 1 ? '' : 's'} original{archive?.files === 1 ? '' : 'aux'}</span></div>
          <div className="kb-archive-size"><strong>{archive ? formatBytes(archive.size_bytes) : '—'}</strong><span>sur {archive ? formatBytes(archive.max_bytes) : '—'} autorisés</span>
            <div className="kb-archive-bar" aria-hidden="true"><span style={{ width: `${archive?.max_bytes ? Math.min(100, (archive.size_bytes / archive.max_bytes) * 100) : 0}%` }} /></div></div>
        </div>
        <div className="kb-actions">
          <button className="button secondary" disabled={busy || !archive?.enabled} onClick={() => act('/admin/kb/archive/export', { method: 'POST' }, 'Éléments déjà indexés copiés dans l’archive (texte seulement : leurs fichiers d’origine n’étaient pas conservés).')}><HardDrive size={15} />Archiver les éléments déjà indexés</button>
          <button className="button secondary" disabled={busy || !archive?.enabled} onClick={() => act('/admin/kb/archive/restore', { method: 'POST' }, 'Restauration lancée : les éléments manquants sont recréés et leurs vecteurs recalculés.')}><RefreshCw size={15} />Restaurer depuis l’archive</button>
        </div>
      </>}
    </section>

    <section className="panel kb-jobs-panel">
      <div className="panel-head"><div><h2>Dernières recherches</h2><p>Une carte par recherche : ticket concerné, requête envoyée au portail et sort de chaque résultat.</p></div><ClipboardList size={19} className="muted-icon" /></div>
      <div className="kb-filters">
        {[['all', 'Toutes'], ['success', 'Réussies'], ['failed', 'Échecs'], ['skipped', 'Ignorées']].map(([key, label]) => <button key={key} type="button" className={filter === key ? 'active' : ''} onClick={() => { setFilter(key); setJobsPage(1) }}>{label} <span>{counts[key] ?? 0}</span></button>)}
      </div>
      {visibleJobs.length ? <div className="kb-job-list">
        {visibleJobs.map((job) => {
          const open = expanded.includes(job.id)
          return <article className={`kb-job ${job.status}`} key={job.id}>
            <div className="kb-job-head" role="button" tabIndex={0} aria-expanded={open}
              onClick={() => setExpanded((current) => open ? current.filter((id) => id !== job.id) : [...current, job.id])}
              onKeyDown={(event) => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); setExpanded((current) => open ? current.filter((id) => id !== job.id) : [...current, job.id]) } }}>
              <div className="kb-job-main">
                <div className="kb-job-title" onClick={(event) => event.stopPropagation()}>{kbBadge(kbJobStatuses, job.status)}<KBJobTitle job={job} onOpenTicket={onOpenTicket} /></div>
                <div className="kb-job-query">
                  {job.query
                    ? <><Search size={13} /><code>{job.query}</code><small>{job.query_source === 'llm' ? 'requête générée par l’IA' : 'requête générée par règles'}</small></>
                    : <em>{job.error_code ? `${job.error_code}${job.error ? ` — ${job.error}` : ''}` : 'Aucune requête envoyée'}</em>}
                </div>
              </div>
              <KBJobFacts job={job} />
              <ChevronDown size={16} className={`kb-chevron ${open ? 'open' : ''}`} />
            </div>
            {open && <div className="kb-job-body">
              {job.error && <div className="kb-job-error"><AlertCircle size={14} /><span><b>{job.error_code || 'Erreur'}</b> — {job.error}</span></div>}
              <KBResultList results={job.results} />
              <div className="kb-job-actions"><button type="button" className="button secondary" onClick={() => debugJob(job.id)}><Activity size={14} />Déboguer cette recherche</button></div>
            </div>}
          </article>
        })}
      </div> : <div className="empty-state compact">{status ? (filter !== 'all' ? 'Aucune recherche pour ce filtre.' : 'Aucune recherche KB pour le moment.') : 'Chargement…'}</div>}
      <Pagination page={jobsPage} pageSize={jobsPageSize} total={status?.jobs_total || 0} sizes={[10, 25, 50, 100]} label="recherches" setPage={setJobsPage} setPageSize={(size) => { setJobsPageSizeState(size); rememberPageSize('kb-jobs', size); setJobsPage(1) }} />
    </section>

    <section className="panel kb-debug-panel" id="kb-debug">
      <div className="panel-head"><div><h2>Console de débogage</h2><p>Choisissez une recherche à gauche : son détail et le journal du navigateur sur la même période s’affichent à droite.</p></div><button className="button secondary" onClick={() => { setShowDebug((current) => !current); setDebug(null) }}>{showDebug ? 'Masquer' : 'Afficher'}</button></div>
      {showDebug && (debug ? <div className="kb-debug">
        <nav className="kb-debug-jobs" aria-label="Recherches">
          {debug.jobs.length ? debug.jobs.map((job) => <button type="button" key={job.id} className={`kb-debug-job ${job.status} ${job.id === selectedJobId ? 'selected' : ''}`} onClick={() => { setSelectedJobId(job.id); setDebugTab('search') }}>
            <span className="kb-dot" />
            <span className="kb-debug-job-text">
              <b>{job.kind === 'reindex' ? 'Réindexation' : (job.ticket_reference || 'Ticket supprimé')}</b>
              <small>{job.query || job.error_code || '—'}</small>
            </span>
            <time>{relativeTime(job.created_at)}</time>
          </button>) : <p className="field-hint">Aucune recherche enregistrée.</p>}
          <Pagination page={debugPage} pageSize={12} total={debug.jobs_total || 0} sizes={[12]} compact label="recherches" setPage={setDebugPage} />
        </nav>
        <div className="kb-debug-detail">
          <div className="kb-tabs">
            <button type="button" className={debugTab === 'search' ? 'active' : ''} onClick={() => setDebugTab('search')}>Recherche sélectionnée</button>
            <button type="button" className={debugTab === 'browser' ? 'active' : ''} onClick={() => setDebugTab('browser')}>Journal complet du navigateur ({debug.browser_logs.length})</button>
          </div>
          {debugTab === 'search' && (selectedJob ? <div className="kb-debug-body">
            <header className="kb-debug-header">
              <div className="kb-job-title">{kbBadge(kbJobStatuses, selectedJob.status)}<KBJobTitle job={selectedJob} onOpenTicket={onOpenTicket} /></div>
              <small>{date(selectedJob.created_at)}{selectedJob.duration_ms ? ` · ${(selectedJob.duration_ms / 1000).toFixed(1)} s` : ''}</small>
            </header>
            <dl className="kb-facts">
              <div><dt>Requête envoyée</dt><dd>{selectedJob.query ? <code>{selectedJob.query}</code> : '—'}{selectedJob.query_source ? <small> · {selectedJob.query_source === 'llm' ? 'générée par l’IA' : 'générée par règles'}</small> : null}</dd></div>
              <div><dt>Résultats</dt><dd>{selectedJob.hits} trouvé{selectedJob.hits > 1 ? 's' : ''} · {selectedJob.new_documents} indexé{selectedJob.new_documents > 1 ? 's' : ''} · {selectedJob.skipped_documents} déjà connu{selectedJob.skipped_documents > 1 ? 's' : ''}</dd></div>
              {selectedJob.failed_selector && <div className="bad"><dt>Sélecteur CSS en échec</dt><dd><code>{selectedJob.failed_selector}</code></dd></div>}
              {selectedJob.error && <div className="bad"><dt>Erreur{selectedJob.error_code ? ` (${selectedJob.error_code})` : ''}</dt><dd>{selectedJob.error}</dd></div>}
            </dl>
            <h4 className="kb-section-title">Résultats</h4>
            <KBResultList results={selectedJob.results} />
            {selectedJob.urls?.length > 0 && <><h4 className="kb-section-title">URLs consultées</h4><ul className="kb-urls">{selectedJob.urls.map((url) => <li key={url}><a href={url} target="_blank" rel="noreferrer">{url}</a></li>)}</ul></>}
            <h4 className="kb-section-title">Journal du navigateur pendant cette recherche <span>{jobLogs.length}</span></h4>
            {jobLogs.length ? <ul className="kb-log">{jobLogs.map((entry, index) => <KBLogLine key={index} entry={entry} />)}</ul> : <p className="field-hint">Aucune entrée sur cette période (le journal ne garde que les derniers évènements et se vide au redémarrage du navigateur).</p>}
          </div> : <div className="empty-state compact">Sélectionnez une recherche.</div>)}
          {debugTab === 'browser' && (debug.browser_logs.length
            ? <ul className="kb-log kb-log-full">{logPager.items.map((entry, index) => <KBLogLine key={index} entry={entry} />)}</ul>
            : <p className="field-hint">Aucune entrée (navigateur hors ligne ou module désactivé).</p>)}
          {debugTab === 'browser' && <Pagination {...logPager} sizes={[25, 50, 100, 200]} label="évènements" />}
        </div>
      </div> : <div className="empty-state compact">Chargement du journal…</div>)}
    </section>
  </div>
}

function SupervisionPage({ overview }) {
  const services = overview?.services || []
  const metrics = overview?.metrics || {}

  function statusClass(status) {
    if (status === 'healthy') return 'green'
    if (status === 'warning' || status === 'disabled') return 'amber'
    return 'red'
  }

  function statusLabel(status) {
    if (status === 'healthy') return 'OK'
    if (status === 'warning') return 'Attention'
    if (status === 'disabled') return 'Désactivé'
    return 'Erreur'
  }

  return <div className="supervision-page">
    <div className="welcome-row"><div><span className="eyebrow">SUPERVISION</span><h1>Console d’administration</h1><p>Suivez l’état des services et les indicateurs de support en temps réel.</p></div></div>
    <section className="supervision-metrics">
      <div className="stat-card"><div className="stat-icon blue"><Activity size={18} /></div><span className="stat-label">Incidents ouverts</span><strong className="stat-number">{metrics.open_tickets ?? 0}</strong><span className="stat-hint">Demandes en cours de prise en charge</span></div>
      <div className="stat-card"><div className="stat-icon green"><Users size={18} /></div><span className="stat-label">Collaborateurs</span><strong className="stat-number">{(metrics.staff_count ?? 0) + (metrics.customer_count ?? 0)}</strong><span className="stat-hint">Agents, admins et clients actifs</span></div>
    </section>
    <section className="panel supervision-panel">
      <div className="panel-head"><div><h2>État des services</h2><p>{overview ? (overview.status === 'ok' ? 'Tous les composants sont stables.' : 'Un ou plusieurs services demandent l’attention de l’équipe.') : 'Chargement…'}</p></div><ShieldCheck size={19} className="muted-icon" /></div>
      <div className="service-list">
        {services.length === 0 ? <div className="empty-state compact">Chargement de la supervision…</div> : services.map((service) => (
          <div className={`service-row ${service.status}`} key={service.name}>
            <div className="service-topline">
              <div>
                <strong>{service.name === 'database' ? 'Base de données' : service.name === 'api' ? 'API support' : service.name === 'ollama' ? 'Ollama (génération)' : service.name === 'embedding' ? 'Ollama (embeddings)' : service.name === 'anthropic' ? 'Anthropic / Claude' : service.name === 'rag' ? 'Qdrant (RAG documentaire)' : service.name === 'kb' ? 'Navigateur KB constructeur' : 'File de support'}</strong>
                <p>{service.detail}</p>
              </div>
              <span className={`badge ${statusClass(service.status)}`}>{statusLabel(service.status)}</span>
            </div>
          </div>
        ))}
      </div>
    </section>
  </div>
}

export default App
