import { useEffect, useState } from 'react'
import { useStore } from '../store'

export default function ChatPage() {
  const { documents, refreshDocuments, chat, ask } = useStore()
  const [question, setQuestion] = useState('')
  const [selected, setSelected] = useState<string[]>([])

  useEffect(() => {
    void refreshDocuments()
  }, [refreshDocuments])

  const ready = documents.filter((d) => d.status === 'ready')
  const toggle = (id: string) =>
    setSelected((s) => (s.includes(id) ? s.filter((x) => x !== id) : [...s, id]))

  return (
    <div>
      <h1>Chat</h1>
      <form
        onSubmit={(e) => {
          e.preventDefault()
          if (question.trim()) void ask(question.trim(), selected)
        }}
      >
        <fieldset>
          <legend>Search in (none selected = all documents)</legend>
          {ready.length === 0 && <p>No ready documents.</p>}
          {ready.map((d) => (
            <label key={d.id}>
              <input
                type="checkbox"
                checked={selected.includes(d.id)}
                onChange={() => toggle(d.id)}
              />
              {d.filename}
            </label>
          ))}
        </fieldset>
        <label>
          Question
          <input value={question} onChange={(e) => setQuestion(e.target.value)} />
        </label>
        <button type="submit" disabled={chat.pending || !question.trim()}>
          {chat.pending ? 'Thinking…' : 'Ask'}
        </button>
      </form>

      {chat.error && (
        <p role="alert" className="error">
          {chat.error}
        </p>
      )}
      {chat.response && (
        <section aria-label="answer">
          <h2>Answer</h2>
          <p className="answer">{chat.response.answer}</p>
          <h3>Sources</h3>
          {chat.response.sources.length === 0 && <p>No sources.</p>}
          <ol>
            {chat.response.sources.map((s, i) => (
              <li key={`${s.document_id}-${s.chunk_index}-${i}`}>
                <strong>
                  {s.filename} · chunk {s.chunk_index}
                </strong>
                <small> (doc {s.document_id.slice(0, 8)})</small>
                <p>{s.text}</p>
              </li>
            ))}
          </ol>
        </section>
      )}
    </div>
  )
}
