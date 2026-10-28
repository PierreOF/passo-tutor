import { Fragment } from 'react'
import type { Message } from '../domain/models'
import { MessageAvatar } from './MessageAvatar'
import { TutorMessageContent } from './TutorMessageContent'
interface ConversationMessage {
 id: string; role: 'student' | 'tutor'; text: string; createdAt?: string; kind?: Message['kind'];
 code?: string; pending?: boolean; responseMissing?: boolean; codeUnavailable?: boolean; turn?: number; probeIndex?: number
}
const kindLabels = { question: 'Pergunta orientadora', hint: 'Pista para avançar', pseudocode: 'Plano em pseudocódigo', partial: 'Exemplo parcial', fallback: 'Redirecionamento seguro', out_of_scope: 'Fora do escopo da disciplina' }
export function ConversationMessages({ messages, profileId, studentName, review = false, preparing = false }: {
 messages: ConversationMessage[]; profileId?: string | null; studentName?: string; review?: boolean; preparing?: boolean
}) {
 const name = studentName ?? (review && profileId ? 'Aluno simulado · ' + profileId : 'Aluno')
 // Uma nova dúvida depois de uma sondagem encerrada abre outra sondagem na mesma conversa.
 const startsProbe = (index: number) => index > 0 && messages[index].probeIndex != null && messages[index - 1].probeIndex != null && messages[index].probeIndex !== messages[index - 1].probeIndex
 return <div className="conversation-messages">{messages.map((message, index) => <Fragment key={message.id}>{startsProbe(index) && <div className="probe-separator" role="separator">Nova dúvida</div>}<article className={'conversation-message ' + message.role}>
  <MessageAvatar role={message.role} profileId={profileId}/>
  <div className="conversation-bubble">
   <header className="conversation-author"><strong>{message.role === 'tutor' ? 'Passo' : name}</strong>
    {message.turn != null && <small>{message.turn === 0 ? 'abertura' : 'turno ' + message.turn}</small>}
    {message.createdAt && <time dateTime={message.createdAt}>{Number.isNaN(Date.parse(message.createdAt)) ? message.createdAt : new Date(message.createdAt).toLocaleString('pt-BR')}</time>}
   </header>
   {message.kind && message.kind !== 'question' && <div className="message-kind">{kindLabels[message.kind]}</div>}
   {message.pending && <span className="review-badge">Envio pendente de confirmação</span>}
   {message.role === 'tutor' ? <TutorMessageContent text={message.text}/> : <p className="preserved-text">{message.text}</p>}
   {message.code && <details open><summary>Código enviado</summary><pre><code>{message.code}</code></pre></details>}
   {message.codeUnavailable && <p className="review-notice">Conteúdo do código não preservado neste artefato.</p>}
   {message.responseMissing && <p className="review-notice">Resposta não registrada</p>}
  </div>
 </article></Fragment>)}
 {preparing && <div className="conversation-message tutor" role="status"><MessageAvatar role="tutor"/><div className="conversation-bubble"><strong>Passo</strong><div className="typing"><span/><span/><span/><small>Preparando uma pergunta…</small></div></div></div>}
 </div>
}
