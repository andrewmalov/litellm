"use client";

import { useChatShell } from "@/contexts/ChatShellContext";
import OrganizationBudgetCard from "@/components/organization/OrganizationBudgetCard";
import KeysPanel from "@/components/chat/KeysPanel";

export default function ApiKeysPage() {
  const { accessToken, userId, premiumUser } = useChatShell();

  return (
    <div className="flex-1 min-h-0 overflow-auto w-full py-8 px-8">
      <div className="mb-6">
        <OrganizationBudgetCard accessToken={accessToken} userId={userId} />
      </div>
      <KeysPanel accessToken={accessToken} userId={userId} premiumUser={premiumUser} />
    </div>
  );
}
