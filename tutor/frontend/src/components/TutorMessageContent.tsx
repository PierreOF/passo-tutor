function inlineCode(text: string) {
  return text.split(/(`[^`\n]+`)/g).map((part, index) =>
    /^`[^`\n]+`$/.test(part)
      ? <code key={index}>{part.slice(1, -1)}</code>
      : part,
  )
}

export function TutorMessageContent({ text }: { text: string }) {
  return text.split(/(```[\s\S]*?```)/g).filter(Boolean).map((section, index) => {
    const block = section.match(/^```(?:[a-zA-Z0-9+-]+)?\n?([\s\S]*?)```$/)
    return block
      ? <pre key={index}><code>{block[1].trim()}</code></pre>
      : <p key={index}>{inlineCode(section)}</p>
  })
}
