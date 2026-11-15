import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { AnswerText } from '../components/AnswerText'
import { DocumentPicker } from '../components/DocumentPicker'
import { Icon } from '../components/Icon'
import { RetrievalOptions } from '../components/RetrievalOptions'
import { SourceList } from '../components/SourceList'
import { Alert, EmptyState, PageHeader } from '../components/ui'
import { queryTerms, termRegExp } from '../citations'
import { loadPrefs, requestOverrides, savePrefs, type RetrievalPrefs } from '../retrieval'
import { useStore } from '../store'
import { useCan } from '../store/auth'

export default function ChatPage() {
  const { documents, refreshDocuments, chat, ask, stopChat } = useStore()
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

  const canOpenDocs = useCan('documents:read')
  const [flash, setFlash] = useState<number | null>(null)
  const flashTimer = useRef<number | undefined>(undefined)
  useEffect(() => () => window.clearTimeout(flashTimer.current), [])
  const re = useMemo(() => termRegExp(queryTerms(chat.question)), [chat.question])
  /** Scroll to the source row, move focus there and flash it. */
  const jump = useCallback((n: number) => {
    const el = document.getElementById(`src-${n}`)
    if (!el) return
    const calm = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
    el.scrollIntoView?.({ behavior: calm ? 'auto' : 'smooth', block: 'center' })
    el.focus({ preventScroll: true })
    setFlash(n)
    window.clearTimeout(flashTimer.current)
    flashTimer.current = window.setTimeout(() => setFlash(null), 2200)
  }, [])

  const submit = () => {
    if (question.trim() && !chat.pending) {
      const ids = new Set(documents.filter((d) => d.status === 'ready').map((d) => d.id))
      void ask(
        question.trim(),
        selected.filter((id) => ids.has(id)),
        requestOverrides(prefs),
      )
      setQuestion('')
    }
  }
  const started =
    chat.pending || chat.response !== null || chat.error !== null || chat.stopped === true

  return (
    <div className="chat">
      <PageHeader
        title="Chat"
        description="Ask questions in plain language; answers cite the passages they came from."
      />

      <div className="conversation" aria-live="polite">
        {!started && (
          <EmptyState icon="sparkles" title="Ask your documents anything">
            Pick documents below to narrow the search, or leave &ldquo;All documents&rdquo; to
            search the whole library.
          </EmptyState>
        )}
        {started && (
          <div className="msg msg-user">
            <div className="bubble">{chat.question}</div>
          </div>
        )}
        {chat.pending && !chat.response && (
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
        {chat.stopped && !chat.response && (
          <div className="msg msg-bot">
            <p className="muted small" role="status">
              Stopped.
            </p>
          </div>
        )}
        {chat.response && (
          <div className="msg msg-bot">
            <section
              aria-label="answer"
              className="card bot-card"
              aria-busy={chat.streaming ? 'true' : undefined}
            >
              <div className="bot-label">
                <span className="brand-mark brand-mark-sm">
                  <Icon name="logo" size={12} />
                </span>
                Answer
              </div>
              <div className={chat.streaming ? 'answer is-streaming' : 'answer'}>
                <AnswerText
                  text={chat.response.answer}
                  sources={chat.response.sources}
                  re={re}
                  onCite={jump}
                />
                {chat.streaming && chat.response.answer === '' && (
                  <span className="typing" aria-label="Writing the answer">
                    <i />
                    <i />
                    <i />
                  </span>
                )}
              </div>
              {chat.stopped && (
                <p className="muted small" role="status">
                  Stopped. {chat.response.answer ? 'The answer above is partial.' : ''}
                </p>
              )}
              {chat.done?.cache === 'hit' && (
                <p className="muted small">Served from the answer cache.</p>
              )}
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
                  <SourceList
                    sources={chat.response.sources}
                    re={re}
                    flash={flash}
                    canOpen={canOpenDocs}
                    onJump={jump}
                  />
                </>
              )}
            </section>
          </div>
        )}
        {chat.error && (
          <div className="msg msg-bot">
            <Alert>{chat.error}</Alert>
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
        <div className="composer-tools">
          <DocumentPicker documents={documents} selected={selected} onChange={setSelected} />
          <RetrievalOptions value={prefs} onChange={changePrefs} />
        </div>
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
          {chat.pending ? (
            <button type="button" className="btn" onClick={stopChat}>
              Stop
            </button>
          ) : (
            <button type="submit" className="btn btn-primary" disabled={!question.trim()}>
              <Icon name="send" size={15} />
              Ask
            </button>
          )}
        </div>
      </form>
    </div>
  )
}
