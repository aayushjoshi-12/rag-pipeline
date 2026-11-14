import { useState, type RefObject } from 'react'
import { Icon } from './Icon'

const size = (n: number): string =>
  n >= 1_048_576 ? `${(n / 1_048_576).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1024))} KB`

/** Drag-and-drop target wrapping a real, keyboard-focusable file input (named by `label`). */
export function Dropzone({
  inputRef,
  label,
  multiple = false,
  prompt,
  hint = 'PDF, DOCX, TXT or Markdown',
}: {
  inputRef: RefObject<HTMLInputElement | null>
  label: string
  multiple?: boolean
  prompt: string
  hint?: string
}) {
  const [over, setOver] = useState(false)
  const [files, setFiles] = useState<File[]>([])
  const sync = () => setFiles(Array.from(inputRef.current?.files ?? []))
  return (
    <div>
      <label
        className={over ? 'dropzone is-over' : 'dropzone'}
        onDragOver={(e) => {
          e.preventDefault()
          setOver(true)
        }}
        onDragLeave={() => setOver(false)}
        onDrop={(e) => {
          e.preventDefault()
          setOver(false)
          if (inputRef.current && e.dataTransfer.files.length > 0) {
            inputRef.current.files = e.dataTransfer.files
            sync()
          }
        }}
      >
        <input
          ref={inputRef}
          className="sr-only"
          type="file"
          aria-label={label}
          multiple={multiple}
          accept=".pdf,.docx,.txt,.md"
          onChange={sync}
        />
        <span className="dropzone-icon">
          <Icon name="upload" size={22} />
        </span>
        <span className="dropzone-title">{prompt}</span>
        <span className="dropzone-hint">{hint}</span>
      </label>
      {files.length > 0 && (
        <ul className="file-list" aria-label="selected files">
          {files.map((f) => (
            <li key={f.name + f.size}>
              <Icon name="file" size={15} />
              <span className="truncate" title={f.name}>
                {f.name}
              </span>
              <span className="muted small">{size(f.size)}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
