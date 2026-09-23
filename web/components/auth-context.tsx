"use client";

import { Amplify } from "aws-amplify";
import {
  fetchAuthSession,
  getCurrentUser,
  signIn,
  signOut,
} from "aws-amplify/auth";
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";

interface AuthContextValue {
  accessToken: string | null;
  loading: boolean;
  error: string | null;
  login(username: string, password: string): Promise<void>;
  logout(): Promise<void>;
  refresh(): Promise<string | null>;
}

const AuthContext = createContext<AuthContextValue | null>(null);

let configured = false;
function configureAmplify() {
  if (configured) return;
  const userPoolId = process.env.NEXT_PUBLIC_COGNITO_USER_POOL_ID;
  const userPoolClientId = process.env.NEXT_PUBLIC_COGNITO_CLIENT_ID;
  if (userPoolId && userPoolClientId) {
    Amplify.configure({
      Auth: {
        Cognito: {
          userPoolId,
          userPoolClientId,
        },
      },
    });
    configured = true;
  }
}

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [accessToken, setAccessToken] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    configureAmplify();
    if (!configured) {
      setError("Cognito public environment variables are not configured.");
      setLoading(false);
      return null;
    }
    try {
      await getCurrentUser();
      const session = await fetchAuthSession();
      const token = session.tokens?.accessToken?.toString() ?? null;
      setAccessToken(token);
      setError(null);
      return token;
    } catch {
      setAccessToken(null);
      return null;
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const login = useCallback(
    async (username: string, password: string) => {
      configureAmplify();
      setError(null);
      try {
        const result = await signIn({ username, password });
        if (!result.isSignedIn) {
          throw new Error(
            `Cognito requires an additional step: ${result.nextStep.signInStep}`,
          );
        }
        await refresh();
      } catch (caught) {
        const message =
          caught instanceof Error ? caught.message : "Sign in failed";
        setError(message);
        throw caught;
      }
    },
    [refresh],
  );

  const logout = useCallback(async () => {
    await signOut();
    setAccessToken(null);
  }, []);

  const value = useMemo(
    () => ({ accessToken, loading, error, login, logout, refresh }),
    [accessToken, loading, error, login, logout, refresh],
  );
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const value = useContext(AuthContext);
  if (!value) throw new Error("useAuth must be used inside AuthProvider");
  return value;
}
