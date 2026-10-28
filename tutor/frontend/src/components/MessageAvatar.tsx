import { useState } from 'react'
import passo from '../assets/avatars/passo.svg?no-inline'
import generic from '../assets/avatars/student-default.svg?no-inline'
import p01 from '../assets/avatars/student-p01.svg?no-inline'
import p02 from '../assets/avatars/student-p02.svg?no-inline'
import p03 from '../assets/avatars/student-p03.svg?no-inline'
import p04 from '../assets/avatars/student-p04.svg?no-inline'
import p05 from '../assets/avatars/student-p05.svg?no-inline'
const profiles: Record<string, string> = { P01: p01, P02: p02, P03: p03, P04: p04, P05: p05 }
export function MessageAvatar({ role, profileId }: { role: 'student' | 'tutor'; profileId?: string | null }) {
 const src = role === 'tutor' ? passo : (profileId && Object.prototype.hasOwnProperty.call(profiles, profileId) ? profiles[profileId] : generic)
 const [failedSrc, setFailedSrc] = useState<string | null>(null)
 return <span className={'message-avatar ' + role} aria-hidden="true">{failedSrc === src ? <span>{role === 'tutor' ? 'P' : 'A'}</span> : <img src={src} alt="" width="40" height="40" onError={() => setFailedSrc(src)} />}</span>
}
