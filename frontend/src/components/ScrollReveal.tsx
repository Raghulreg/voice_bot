import { useEffect, useRef, useState, type CSSProperties, type ReactNode } from 'react'

export type RevealVariant = 'up' | 'down' | 'left' | 'right' | 'scale' | 'blur-up'

interface ScrollRevealProps {
  children: ReactNode
  variant?: RevealVariant
  delay?: number
  duration?: number
  className?: string
  threshold?: number
  once?: boolean
}

const variantClass: Record<RevealVariant, string> = {
  up: 'scroll-reveal-up',
  down: 'scroll-reveal-down',
  left: 'scroll-reveal-left',
  right: 'scroll-reveal-right',
  scale: 'scroll-reveal-scale',
  'blur-up': 'scroll-reveal-blur-up',
}

export default function ScrollReveal({
  children,
  variant = 'up',
  delay = 0,
  duration = 900,
  className = '',
  threshold = 0.12,
  once = true,
}: ScrollRevealProps) {
  const ref = useRef<HTMLDivElement>(null)
  const [visible, setVisible] = useState(false)

  useEffect(() => {
    const el = ref.current
    if (!el) return

    const prefersReduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches
    if (prefersReduced) {
      setVisible(true)
      return
    }

    const observer = new IntersectionObserver(
      ([entry]) => {
        if (entry.isIntersecting) {
          setVisible(true)
          if (once) observer.disconnect()
        } else if (!once) {
          setVisible(false)
        }
      },
      { threshold, rootMargin: '0px 0px -6% 0px' }
    )

    observer.observe(el)
    return () => observer.disconnect()
  }, [threshold, once])

  const style: CSSProperties = {
    transitionDuration: `${duration}ms`,
    transitionDelay: visible ? `${delay}ms` : '0ms',
  }

  return (
    <div
      ref={ref}
      style={style}
      className={`scroll-reveal ${variantClass[variant]} ${visible ? 'scroll-reveal-visible' : ''} ${className}`}
    >
      {children}
    </div>
  )
}
