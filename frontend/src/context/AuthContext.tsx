import { createContext, useContext, useEffect, useState, type ReactNode } from 'react'

export interface User {
  name: string
  email: string
}

interface AuthContextValue {
  user: User | null
  isLoading: boolean
  login: (email: string, password: string) => Promise<void>
  signup: (name: string, email: string, password: string) => Promise<void>
  logout: () => void
}

const AuthContext = createContext<AuthContextValue | null>(null)

const USER_KEY = 'voicecall_user'
const USERS_KEY = 'voicecall_users'

function loadUser(): User | null {
  const raw = localStorage.getItem(USER_KEY)
  if (!raw) return null
  try {
    return JSON.parse(raw) as User
  } catch {
    return null
  }
}

function loadUsers(): Record<string, { name: string; password: string }> {
  const raw = localStorage.getItem(USERS_KEY)
  if (!raw) return {}
  try {
    return JSON.parse(raw)
  } catch {
    return {}
  }
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null)
  const [isLoading, setIsLoading] = useState(true)

  useEffect(() => {
    setUser(loadUser())
    setIsLoading(false)
  }, [])

  const login = async (email: string, password: string) => {
    const users = loadUsers()
    const record = users[email.toLowerCase()]
    if (!record || record.password !== password) {
      throw new Error('Invalid email or password')
    }
    const nextUser = { name: record.name, email: email.toLowerCase() }
    localStorage.setItem(USER_KEY, JSON.stringify(nextUser))
    setUser(nextUser)
  }

  const signup = async (name: string, email: string, password: string) => {
    const key = email.toLowerCase().trim()
    if (!name.trim() || !key || password.length < 6) {
      throw new Error('Please fill all fields. Password must be at least 6 characters.')
    }
    const users = loadUsers()
    if (users[key]) {
      throw new Error('An account with this email already exists')
    }
    users[key] = { name: name.trim(), password }
    localStorage.setItem(USERS_KEY, JSON.stringify(users))
    const nextUser = { name: name.trim(), email: key }
    localStorage.setItem(USER_KEY, JSON.stringify(nextUser))
    setUser(nextUser)
  }

  const logout = () => {
    localStorage.removeItem(USER_KEY)
    setUser(null)
  }

  return (
    <AuthContext.Provider value={{ user, isLoading, login, signup, logout }}>
      {children}
    </AuthContext.Provider>
  )
}

export function useAuth() {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth must be used within AuthProvider')
  return ctx
}
