"use client";

import { useAuth } from "@/contexts/AuthContext";
import LoadingScreen from "@/components/common_components/LoadingScreen";
import { Suspense, useEffect } from "react";
import { useRouter } from "next/navigation";
import useAuthorized from "@/app/(dashboard)/hooks/useAuthorized";
import { useUISettings } from "@/app/(dashboard)/hooks/uiSettings/useUISettings";
import Navbar from "@/components/navbar";
import { ThemeProvider } from "@/contexts/ThemeContext";
import { ChatShellProvider } from "@/contexts/ChatShellContext";
import ChatShell from "@/components/chat/ChatShell";
import { uiHref } from "@/utils/uiHref";

// The nuqs Next adapter uses useSearchParams, so keep the chat tree behind Suspense.
function ChatLayoutContent({ children }: { children: React.ReactNode }) {
  const { accessToken, userRole, userId, userEmail, premiumUser } = useAuthorized();
  const { data: uiSettings, isLoading: isUISettingsLoading } = useUISettings();
  const router = useRouter();

  const chatEnabled = Boolean(uiSettings?.values?.enable_chat_ui);
  const blocked = !isUISettingsLoading && !chatEnabled;

  useEffect(() => {
    if (blocked) router.replace(uiHref(""));
  }, [blocked, router]);

  if (isUISettingsLoading || blocked) return null;

  return (
    <ThemeProvider accessToken={accessToken}>
      <div className="flex h-screen flex-col">
        <Navbar accessToken={accessToken} isPublicPage={false} />
        <div className="min-h-0 flex-1">
          <ChatShellProvider
            accessToken={accessToken ?? ""}
            userId={userId ?? ""}
            userEmail={userEmail ?? ""}
            userRole={userRole ?? ""}
            premiumUser={premiumUser ?? false}
          >
            <ChatShell>{children}</ChatShell>
          </ChatShellProvider>
        </div>
      </div>
    </ThemeProvider>
  );
}

function ChatAuthGate({ children }: { children: React.ReactNode }) {
  const { authLoading, passwordResetRequired } = useAuth();
  const router = useRouter();

  useEffect(() => {
    if (!authLoading && passwordResetRequired) router.replace(uiHref("change-password"));
  }, [authLoading, passwordResetRequired, router]);

  if (authLoading || passwordResetRequired) return <LoadingScreen />;
  return <ChatLayoutContent>{children}</ChatLayoutContent>;
}

export default function ChatLayout({ children }: { children: React.ReactNode }) {
  return (
    <Suspense>
      <ChatAuthGate>{children}</ChatAuthGate>
    </Suspense>
  );
}
