import { fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AuthProvider, useAuth } from "@/contexts/AuthContext";
import { renderWithProviders, testQueryClient } from "@/../tests/test-utils";
import { useLogin } from "./useLogin";

function SessionProbe() {
  const session = useAuth();
  const login = useLogin();
  const state = {
    loading: session.authLoading,
    user: session.userID,
    key: session.accessToken,
    reset: session.passwordResetRequired,
  };
  return (
    <>
      <button onClick={() => login.mutate({ username: "new-member", password: "temporary-password" })}>Sign in</button>
      <output aria-label="session">{JSON.stringify(state)}</output>
    </>
  );
}

function token(claims: Record<string, unknown>): string {
  return `${btoa(JSON.stringify({ alg: "none" }))}.${btoa(JSON.stringify({ ...claims, exp: Date.now() / 1000 + 3600 }))}.test`;
}

afterEach(() => {
  document.cookie = "token=; Max-Age=0; Path=/";
  document.cookie = "token=; Max-Age=0; Path=/ui";
  sessionStorage.clear();
  testQueryClient.clear();
  vi.unstubAllGlobals();
});

describe("local login updates the persistent auth provider", () => {
  it.each([false, true])(
    "adopts the restricted member session after SPA login, previous session: %s",
    async (previous) => {
      document.cookie = previous
        ? `token=${token({ user_id: "previous-admin", key: "old-session", user_role: "proxy_admin" })}; Path=/`
        : "token=; Max-Age=0; Path=/";
      const claims = {
        user_id: "new-member",
        key: "new-session",
        user_role: "internal_user",
        password_reset_required: true,
      };
      const newToken = token(claims);
      vi.stubGlobal(
        "fetch",
        vi.fn(async (input: RequestInfo | URL) => {
          const path = new URL(String(input), "http://localhost").pathname;
          if (path.endsWith("/v2/login")) return Response.json({ token: newToken, redirect_url: "/ui/api-keys/" });
          if (path.endsWith("/litellm-ui-config")) return Response.json({ server_root_path: "", proxy_base_url: "" });
          throw new Error(`Unexpected request: ${path}`);
        }),
      );
      renderWithProviders(
        <AuthProvider>
          <SessionProbe />
        </AuthProvider>,
      );
      await waitFor(() => expect(screen.getByLabelText("session")).toHaveTextContent('"loading":false'));
      fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
      await waitFor(() => expect(screen.getByLabelText("session")).toHaveTextContent('"reset":true'));
      expect(screen.getByLabelText("session")).toHaveTextContent('"user":"new-member"');
      expect(screen.getByLabelText("session")).toHaveTextContent('"key":"new-session"');
      expect(screen.getByLabelText("session")).toHaveTextContent('"loading":false');
    },
  );
});
