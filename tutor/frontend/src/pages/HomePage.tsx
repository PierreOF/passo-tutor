import { ArrowRight, Brain, History, MessageCircleQuestion, ShieldCheck } from 'lucide-react'
import { Link } from 'react-router-dom'
import { MessageAvatar } from '../components/MessageAvatar'

/**
 * Exemplo ilustrativo e fiel ao fluxo do tutor: o aluno pede a solução e o
 * tutor responde a cada fala com uma pergunta socrática, sem código pronto.
 */
const exampleTurns: { role: 'student' | 'tutor'; text: string }[] = [
  { role: 'student', text: 'Me passa o código que soma os números de 1 até N? Preciso entregar hoje.' },
  { role: 'tutor', text: 'Vamos montar juntos. Se N fosse 4, quais números você somaria, e em que ordem?' },
  { role: 'student', text: '1 + 2 + 3 + 4, que dá 10. Mas não sei escrever isso em Python.' },
  { role: 'tutor', text: 'Você somou um número de cada vez. O que precisou guardar de uma soma para a seguinte?' },
]

export function HomePage() {
  return <>
    <section className="hero section-wrap">
      <div className="hero-copy">
        <p className="eyebrow">Tutor socrático · Programação I</p>
        <h1>Uma pergunta.<br/>Um novo <em>passo</em>.</h1>
        <p>Converse com um tutor socrático sobre programação introdutória. Apresente sua dúvida ou tentativa: em vez de entregar a solução, ele faz perguntas que ajudam você a dar o próximo passo.</p>
        <div className="hero-actions"><Link className="button" to="/chat">Conversar com o tutor <ArrowRight size={18}/></Link></div>
        <p className="trust-note">Ferramenta de pesquisa sobre a conformidade técnica de um tutor baseado na arquitetura SELA.</p>
      </div>
      <figure className="hero-demo" aria-labelledby="hero-demo-caption">
        <figcaption className="demo-topbar" id="hero-demo-caption"><span>Exemplo ilustrativo</span><span>Soma de 1 até N</span></figcaption>
        <div className="demo-conversation">
          {exampleTurns.map((turn, index) => <div key={index} className={'conversation-message ' + turn.role}>
            <MessageAvatar role={turn.role} />
            <div className="conversation-bubble"><div className="conversation-author"><strong>{turn.role === 'tutor' ? 'Tutor' : 'Aluno'}</strong></div><p>{turn.text}</p></div>
          </div>)}
        </div>
        <p className="demo-footnote"><ShieldCheck size={16} aria-hidden="true"/> Cada pergunta é validada antes de ser publicada. O tutor não entrega código pronto.</p>
      </figure>
    </section>
    <section className="how-section section-wrap"><div className="section-heading"><div><p className="eyebrow">Como funciona</p><h2>Seu raciocínio guia a conversa.</h2></div></div><div className="feature-grid">
      <article><MessageCircleQuestion/><h3>Apresente sua dúvida</h3><p>Conte o que está tentando resolver e o que já tentou, com ou sem código.</p></article>
      <article><Brain/><h3>Responda a uma pergunta</h3><p>O tutor identifica o que falta no seu raciocínio e responde com uma pergunta, não com a solução.</p></article>
      <article><History/><h3>Avance até resolver</h3><p>As perguntas seguem até a dúvida se resolver, e a conversa fica salva no seu histórico.</p></article>
    </div></section>
  </>
}
