import { Loader2 } from 'lucide-react'
import { Link } from 'react-router-dom'
import type { ButtonHTMLAttributes, ReactNode } from 'react'

type Variant = 'primary' | 'secondary' | 'ghost' | 'danger'
type Size = 'sm' | 'md' | 'lg'

const variants: Record<Variant, string> = {
  primary:
    'bg-brand-800 text-white border border-brand-900 hover:bg-brand-700 active:bg-brand-900 disabled:bg-zinc-200 disabled:text-zinc-600 disabled:border-zinc-200 disabled:shadow-none',
  secondary:
    'bg-white text-slate-800 border border-slate-300 hover:bg-slate-50 hover:border-slate-400 active:bg-slate-100 shadow-sm disabled:text-zinc-400 disabled:border-zinc-200',
  ghost:
    'bg-transparent text-slate-700 hover:bg-slate-100 hover:text-slate-900 disabled:text-zinc-400',
  danger:
    'bg-red-700 text-white hover:bg-red-600 active:bg-red-800 border border-red-800/20 shadow-sm disabled:bg-zinc-200 disabled:text-zinc-500',
}

const sizes: Record<Size, string> = {
  sm: 'h-8 px-3 text-xs gap-1.5 rounded-lg font-semibold',
  md: 'h-9 px-4 text-sm gap-2 rounded-lg font-semibold',
  lg: 'h-10 px-5 text-sm gap-2 rounded-lg font-semibold',
}

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant
  size?: Size
  loading?: boolean
  icon?: ReactNode
  to?: string
}

export default function Button({
  variant = 'primary',
  size = 'md',
  loading,
  icon,
  children,
  className = '',
  disabled,
  to,
  type = 'button',
  ...props
}: ButtonProps) {
  const classes = `inline-flex items-center justify-center transition-all duration-200 focus-visible:shadow-focus disabled:cursor-not-allowed ${variants[variant]} ${sizes[size]} ${className}`

  const content = (
    <>
      {loading ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : icon}
      {children}
    </>
  )

  if (to) {
    return (
      <Link
        to={to}
        className={`${classes}${disabled || loading ? ' pointer-events-none opacity-50' : ''}`}
        aria-disabled={disabled || loading || undefined}
      >
        {content}
      </Link>
    )
  }

  return (
    <button className={classes} disabled={disabled || loading} type={type} {...props}>
      {content}
    </button>
  )
}
