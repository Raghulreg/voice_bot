import type { InputHTMLAttributes, ReactNode, SelectHTMLAttributes } from 'react'

interface FieldProps extends InputHTMLAttributes<HTMLInputElement> {
  label: string
  icon?: ReactNode
  hint?: string
  error?: string
}

export function Field({ label, icon, hint, error, className = '', id, ...props }: FieldProps) {
  const inputId = id || label.toLowerCase().replace(/\s+/g, '-')
  return (
    <div className="space-y-1">
      <label htmlFor={inputId} className="field-label">
        {label}
      </label>
      <div className="relative">
        {icon && (
          <span className="absolute left-2.5 top-1/2 -translate-y-1/2 text-content-subtle pointer-events-none">
            {icon}
          </span>
        )}
        <input
          id={inputId}
          className={`field-input ${icon ? 'pl-8' : ''} ${error ? 'ring-1 ring-red-300' : ''} ${className}`}
          {...props}
        />
      </div>
      {error && <p className="text-[11px] text-red-600">{error}</p>}
      {hint && !error && <p className="text-[11px] text-content-muted">{hint}</p>}
    </div>
  )
}

interface SelectFieldProps extends SelectHTMLAttributes<HTMLSelectElement> {
  label: string
  hint?: string
}

export function SelectField({ label, hint, className = '', id, children, ...props }: SelectFieldProps) {
  const inputId = id || label.toLowerCase().replace(/\s+/g, '-')
  return (
    <div className="space-y-1">
      <label htmlFor={inputId} className="field-label">
        {label}
      </label>
      <select id={inputId} className={`field-input appearance-none cursor-pointer ${className}`} {...props}>
        {children}
      </select>
      {hint && <p className="text-[11px] text-content-muted">{hint}</p>}
    </div>
  )
}

/** @deprecated use Field */
export const FloatingInput = Field
/** @deprecated use SelectField */
export const FloatingSelect = SelectField
