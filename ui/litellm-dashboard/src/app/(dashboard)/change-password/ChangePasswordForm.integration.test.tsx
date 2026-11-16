import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AuthProvider, useAuth } from "@/contexts/AuthContext";
import ChangePasswordForm from "./ChangePasswordForm";

vi.unmock("@/app/(dashboard)/hooks/useAuthorized");

const mockChangePasswordCall = vi.fn();
const mockToastSuccess = vi.fn();
const mockClearTokenCookies = vi.fn();
let mockPasswordResetRequired = false;

vi.mock("@/components/networking", () => ({
  changePasswordCall: (...args: unknown[]) => mockChangePasswordCall(...args),
  getProxyBaseUrl: () => "",
  getUiConfig: async () => ({}),
  setGlobalLitellmHeaderName: vi.fn(),
  sessionLogoutCall: vi.fn().mockResolvedValue({}),
}));

vi.mock("@/app/(dashboard)/hooks/uiConfig/useUIConfig", () => ({
  useUIConfig: () => ({ data: {}, isLoading: false }),
}));

vi.mock("@/lib/toast", () => ({
  toast: {
    success: (...args: unknown[]) => mockToastSuccess(...args),
    fromError: vi.fn(),
  },
}));

vi.mock("@/utils/cookieUtils", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/utils/cookieUtils")>();
  return {
    ...actual,
    clearTokenCookies: () => {
      mockClearTokenCookies();
      actual.clearTokenCookies();
    },
  };
});

function SessionReady() {
  return useAuth().authLoading ? null : <span>Session ready</span>;
}

async function renderForm() {
  const claims = {
    key: "sk-session-token",
    user_id: "member",
    user_role: "internal_user",
    password_reset_required: mockPasswordResetRequired,
    exp: Date.now() / 1000 + 3600,
  };
  document.cookie = `token=e30.${btoa(JSON.stringify(claims))}.test; Path=/`;
  render(
    <AuthProvider>
      <SessionReady />
      <ChangePasswordForm />
    </AuthProvider>,
  );
  await screen.findByText("Session ready");
}

afterEach(() => {
  document.cookie = "token=; Max-Age=0; Path=/";
  document.cookie = "litellm_return_url=; Max-Age=0; Path=/";
});

const fillForm = (values: { current: string; next: string; confirm: string }) => {
  fireEvent.change(screen.getByLabelText("Current Password"), { target: { value: values.current } });
  fireEvent.change(screen.getByLabelText("New Password"), { target: { value: values.next } });
  fireEvent.change(screen.getByLabelText("Confirm New Password"), { target: { value: values.confirm } });
};

const submit = () => fireEvent.click(screen.getByRole("button", { name: "Change Password" }));

describe("ChangePasswordForm", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockPasswordResetRequired = false;
  });

  it("sends the current and new password to the change endpoint and resets on success", async () => {
    mockChangePasswordCall.mockResolvedValue({ user_id: "user-123", message: "Password updated successfully." });
    await renderForm();

    fillForm({ current: "OldP@ssw0rd-2026", next: "NewP@ssw0rd-2026", confirm: "NewP@ssw0rd-2026" });
    submit();

    expect(await screen.findByLabelText("Current Password")).toHaveValue("");
    expect(mockChangePasswordCall).toHaveBeenCalledWith("sk-session-token", "OldP@ssw0rd-2026", "NewP@ssw0rd-2026");
    expect(mockToastSuccess).toHaveBeenCalled();
  });

  it("blocks submission when the confirmation does not match", async () => {
    await renderForm();

    fillForm({ current: "OldP@ssw0rd-2026", next: "NewP@ssw0rd-2026", confirm: "Different-2026" });
    submit();

    expect(await screen.findByText("New passwords do not match")).toBeInTheDocument();
    expect(mockChangePasswordCall).not.toHaveBeenCalled();
  });

  it("shows the proxy's rejection message unwrapped", async () => {
    mockChangePasswordCall.mockRejectedValue(new Error("{'error': 'Current password is incorrect.'}"));
    await renderForm();

    fillForm({ current: "wrong-password", next: "NewP@ssw0rd-2026", confirm: "NewP@ssw0rd-2026" });
    submit();

    expect(await screen.findByText("Current password is incorrect.")).toBeInTheDocument();
    expect(mockToastSuccess).not.toHaveBeenCalled();
  });

  describe("forced password reset", () => {
    it("shows the forced-reset warning only when the session is flagged", async () => {
      mockPasswordResetRequired = true;
      await renderForm();

      expect(screen.getByText(/must be changed before you can use the dashboard/)).toBeInTheDocument();
    });

    it("hides the forced-reset warning for a normal session", async () => {
      await renderForm();

      expect(screen.queryByText(/must be changed before you can use the dashboard/)).not.toBeInTheDocument();
    });

    it("signs the user out to re-login after a successful forced change", async () => {
      mockPasswordResetRequired = true;
      const completed = Promise.withResolvers<{ user_id: string }>();
      mockChangePasswordCall.mockReturnValue(completed.promise);
      const replaceMock = vi.fn();
      const realLocation = window.location;
      Object.defineProperty(window, "location", {
        configurable: true,
        value: { ...realLocation, href: "http://localhost:3000/ui/change-password/", replace: replaceMock },
      });

      try {
        await renderForm();
        fillForm({ current: "OldP@ssw0rd-2026", next: "NewP@ssw0rd-2026", confirm: "NewP@ssw0rd-2026" });
        submit();
        await waitFor(() => expect(screen.getByRole("button", { name: /Change Password/ })).toBeDisabled());
        await act(async () => completed.resolve({ user_id: "user-123" }));

        await waitFor(() => expect(replaceMock).toHaveBeenCalledWith("/ui/login/"));
        expect(mockClearTokenCookies).toHaveBeenCalled();
        await waitFor(() => expect(screen.getByRole("button", { name: "Change Password" })).toBeEnabled());
        expect(replaceMock.mock.calls).toEqual([["/ui/login/"]]);
        expect(document.cookie).not.toContain("litellm_return_url=");
      } finally {
        Object.defineProperty(window, "location", { configurable: true, value: realLocation });
      }
    });
  });
});
