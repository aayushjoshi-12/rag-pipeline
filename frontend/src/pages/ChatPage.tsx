import { useEffect, useState } from 'react'
import { Icon } from '../components/Icon'
import { RetrievalOptions } from '../components/RetrievalOptions'
import { Alert, EmptyState, PageHeader } from '../components/ui'
import {
  loadPrefs,
  requestOverrides,
  savePrefs,
  scoreLabel,
  type RetrievalPrefs,
} from '../retrieval'
import { useStore } from '../store'
import type { Source } from '../types'

function SourceCard({ source, n }: { source: Source; n: number }) {
  const [open, setOpen] = useState(false)
  const long = source.text.length > 240
  const context = source.is_hit === false
  const label = scoreLabel(source)
  return (
    <li className={context ? 'source is-context' : 'source'}>
      <span className="source-n" aria-hidden="true">
        {n}
      </span>
      <div className="source-main">
        <div className="source-head">
          <strong className="truncate" title={source.filename}>
            {source.filename} · chunk {source.chunk_index}
          </strong>
          {!context && (
            <span className="chip chip-static num" title={label.title}>
              {label.text}
            </span>
          )}
          {source.semantic_rank != null && (
            <span className="chip chip-static num" title="Rank in the semantic (meaning) results">
              semantic #{source.semantic_rank}
            </span>
          )}
          {source.keyword_rank != null && (
            <span
              className="chip chip-static num"
              title="Rank in the keyword (exact words) results"
            >
              keyword #{source.keyword_rank}
            </span>
          )}
          {context && (
            <span
              className="chip chip-static"
              title="Neighbouring chunk added for context (not scored)"
            >
              context
            </span>
          )}
        </div>
        <p
          className={open ? 'source-text' : 'source-text clamp'}
          title={`doc ${source.document_id}`}
        >
          {source.text}
        </p>
        {long && (
          <button type="button" className="linklike small" onClick={() => setOpen((v) => !v)}>
            {open ? 'Show less' : 'Show more'}
          </button>
        )}
      </div>
    </li>
  )
}

export default function ChatPage() {
  const { documents, refreshDocuments, chat, ask } = useStore()
  const [question, setQuestion] = useState('')
  const [selected, setSelected] = useState<string[]>([])
  const [prefs, setPrefs] = useState<RetrievalPrefs>(loadPrefs)
  const changePrefs = (next: RetrievalPrefs) => {
    setPrefs(next)
    savePrefs(next)
  }

  useEffect(() => {
    void refreshDocuments()
  }, [refreshDocuments])

  const ready = documents.filter((d) => d.status === 'ready')
  const toggle = (id: string) =>
    setSelected((s) => (s.includes(id) ? s.filter((x) => x !== id) : [...s, id]))
  const submit = () => {
    if (question.trim() && !chat.pending) {
      void ask(question.trim(), selected, requestOverrides(prefs))
      setQuestion('')
    }
  }
  const started = chat.pending || chat.response !== null || chat.error !== null

  return (
    <div className="chat">
      <PageHeader
        title="Chat"
        description="Ask questions in plain language; answers cite the passages they came from."
      />

      <div className="conversation" aria-live="polite">
        {!started && (
          <EmptyState icon="sparkles" title="Ask your documents anything">
            Pick documents below to narrow the search, or leave them all unselected to search the
            whole library.
          </EmptyState>
        )}
        {started && (
          <div className="msg msg-user">
            <div className="bubble">{chat.question}</div>
          </div>
        )}
        {chat.pending && (
          <div className="msg msg-bot" aria-busy="true">
            <div className="card bot-card">
              <p className="thinking">
                <span className="typing" aria-hidden="true">
                  <i />
                  <i />
                  <i />
                </span>
                Searching documents and writing an answer…
              </p>
            </div>
          </div>
        )}
        {chat.error && (
          <div className="msg msg-bot">
            <Alert>{chat.error}</Alert>
          </div>
        )}
        {chat.response && (
          <div className="msg msg-bot">
            <section aria-label="answer" className="card bot-card">
              <div className="bot-label">
                <span className="brand-mark brand-mark-sm">
                  <Icon name="logo" size={12} />
                </span>
                Answer
              </div>
              <div className="answer">{chat.response.answer}</div>
              {chat.response.sources.length === 0 ? (
                <p className="notice">
                  <Icon name="alert" size={16} />
                  No relevant documents were found for this question, so there are no sources.
                </p>
              ) : (
                <>
                  <h2 className="sources-title">
                    Sources <span className="muted num">{chat.response.sources.length}</span>
                  </h2>
                  <ol className="sources">
                    {chat.response.sources.map((s, i) => (
                      <SourceCard
                        key={`${s.document_id}-${s.chunk_index}-${i}`}
                        source={s}
                        n={i + 1}
                      />
                    ))}
                  </ol>
                </>
              )}
            </section>
          </div>
        )}
      </div>

      <form
        className="composer"
        onSubmit={(e) => {
          e.preventDefault()
          submit()
        }}
      >
        <fieldset className="scope">
          <legend>Search in (none selected = all documents)</legend>
          {ready.length === 0 && <span className="muted small">No ready documents.</span>}
          <div className="chips">
            {ready.map((d) => (
              <label key={d.id} className={selected.includes(d.id) ? 'chip is-on' : 'chip'}>
                <input
                  type="checkbox"
                  checked={selected.includes(d.id)}
                  onChange={() => toggle(d.id)}
                />
                <Icon name={selected.includes(d.id) ? 'check' : 'file'} size={13} />
                <span className="truncate" title={d.filename}>
                  {d.filename}
                </span>
              </label>
            ))}
          </div>
        </fieldset>
        <RetrievalOptions value={prefs} onChange={changePrefs} />
        <div className="composer-row">
          <label className="sr-only" htmlFor="chat-question">
            Question
          </label>
          <textarea
            id="chat-question"
            autoComplete="off"
            className="input composer-input"
            rows={1}
            placeholder="Ask a question about your documents…"
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault()
                submit()
              }
            }}
          />
          <button
            type="submit"
            className="btn btn-primary"
            disabled={chat.pending || !question.trim()}
          >
            <Icon name="send" size={15} />
            {chat.pending ? 'Thinking…' : 'Ask'}
          </button>
        </div>
      </form>
    </div>
  )
}
