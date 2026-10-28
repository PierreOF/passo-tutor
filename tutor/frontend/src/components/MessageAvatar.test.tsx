import { cleanup, fireEvent, render } from '@testing-library/react'
import { afterEach, expect, it } from 'vitest'
import { MessageAvatar } from './MessageAvatar'
afterEach(cleanup)
it('keeps profile identity stable and uses a separate tutor image', () => {
 const { container, rerender } = render(<MessageAvatar role="student" profileId="P01" />)
 const image = () => container.querySelector('img')!
 expect(image().src).toContain('student-p01.svg')
 const first = image().src
 rerender(<MessageAvatar role="student" profileId="P02" />)
 expect(image().src).toContain('student-p02.svg')
 rerender(<MessageAvatar role="student" profileId="P01" />)
 expect(image().src).toBe(first)
 rerender(<MessageAvatar role="tutor" profileId="P01" />)
 expect(image().src).toContain('passo.svg')
})
it('falls back for unknown profiles and failed images without breaking identity', () => {
 const { container, rerender } = render(<MessageAvatar role="student" profileId="unknown" />)
 expect(container.querySelector('img')!.src).toContain('student-default.svg')
 fireEvent.error(container.querySelector('img')!)
 expect(container.querySelector('img')).toBeNull()
 expect(container).toHaveTextContent('A')
 rerender(<MessageAvatar role="tutor" />)
 expect(container.querySelector('img')!.src).toContain('passo.svg')
})
