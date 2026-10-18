/** Phone helpers — catch common India country-code mistakes (+1 vs +91). */

export function normalizePhoneInput(phone: string): string {
  return phone.trim().replace(/[\s-]/g, '')
}

/**
 * +19176242215 is often meant to be +919176242215 (India).
 * US +1 followed by 10 digits starting 6–9 matches Indian mobile pattern.
 */
export function suggestIndianNumberFix(phone: string): string | null {
  const cleaned = normalizePhoneInput(phone)
  if (/^\+1[6-9]\d{9}$/.test(cleaned)) {
    return `+91${cleaned.slice(2)}`
  }
  return null
}

export function validatePhoneForCall(phone: string): {
  ok: boolean
  message?: string
  normalized?: string
  suggestedFix?: string
} {
  const cleaned = normalizePhoneInput(phone)

  if (!cleaned) {
    return { ok: false, message: 'Please enter a phone number.' }
  }

  if (!cleaned.startsWith('+')) {
    return {
      ok: false,
      message: 'Include country code with +. India example: +919176242215',
    }
  }

  const suggestedFix = suggestIndianNumberFix(cleaned)
  if (suggestedFix) {
    return {
      ok: false,
      message:
        `Wrong country code: you entered ${cleaned} (US +1). ` +
        `Your Twilio verified number is likely India +91. Try ${suggestedFix} instead.`,
      suggestedFix,
    }
  }

  if (!/^\+[1-9]\d{7,14}$/.test(cleaned)) {
    return {
      ok: false,
      message: 'Invalid format. Use E.164: + country code + number, e.g. +919176242215',
    }
  }

  return { ok: true, normalized: cleaned }
}
