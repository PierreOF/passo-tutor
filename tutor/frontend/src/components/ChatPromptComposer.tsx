import { Send, Square, RotateCcw } from 'lucide-react'
import { FormEvent, useEffect, useRef } from 'react'

interface ChatPromptComposerProps {
  disabled?: boolean
  value: string
  onValueChange: (value: string) => void
  onSubmit: (text: string) => void
  clearOnSubmit?: boolean
  responsePending?: boolean
  canContinue?: boolean
  onStop?: () => void
  onContinue?: () => void
}

export function ChatPromptComposer({
  disabled = false,
  value,
  onValueChange,
  onSubmit,
  clearOnSubmit = true,
  responsePending = false,
  canContinue = false,
  onStop,
  onContinue,
}: ChatPromptComposerProps) {
  const textareaRef = useRef<HTMLTextAreaElement>(null)

  useEffect(() => {
    const textarea = textareaRef.current
    if (!textarea) return
    textarea.style.height = 'auto'
    textarea.style.height = `${Math.min(textarea.scrollHeight, 190)}px`
  }, [value])

  function submit(event: FormEvent) {
    event.preventDefault()
    const text = value.trim()
    if (!text || disabled) return
    onSubmit(text)
    if (clearOnSubmit) onValueChange('')
  }

  return (
    <form className="chat-prompt-composer" onSubmit={submit}>
      <div className="prompt-input-shell">
        <textarea
          ref={textareaRef}
          aria-label="Sua mensagem para o tutor"
          value={value}
          onChange={(event) => onValueChange(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
              event.preventDefault()
              event.currentTarget.form?.requestSubmit()
            }
          }}
          placeholder="Explique sua dúvida ou cole um pequeno trecho de código…"
          rows={1}
          disabled={disabled}
        />
        <div className="prompt-toolbar">
          <span className="prompt-toolbar-spacer" />
          {responsePending ? <button className="prompt-send-button" type="button" onClick={onStop} aria-label="Parar resposta" title="Parar resposta"><Square size={17} fill="currentColor" /></button>
            : canContinue && !value.trim() ? <button className="prompt-send-button" type="button" onClick={onContinue} aria-label="Continuar resposta" title="Continuar resposta"><RotateCcw size={19} /></button>
              : <button className="prompt-send-button" type="submit" disabled={disabled || !value.trim()} aria-label="Enviar mensagem" title="Enviar mensagem"><Send size={19} /></button>}
        </div>
      </div>
      <small>O Passo oferece orientação gradual e não entrega soluções completas.</small>
    </form>
  )
}
