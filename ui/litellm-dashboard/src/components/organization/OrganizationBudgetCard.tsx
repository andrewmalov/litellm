"use client";

import { useQuery } from "@tanstack/react-query";
import { z } from "zod";
import { createApiClient } from "@/lib/http/client";
import { getGlobalLitellmHeaderName, getProxyBaseUrl } from "@/components/networking";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";

const organizationFields = {
  organization_id: z.string(),
  organization_alias: z.string().nullable(),
  max_budget: z.number().nullable(),
  spend: z.number(),
  remaining_budget: z.number().nullable(),
  budget_duration: z.string().nullable(),
  budget_reset_day: z.number().nullable(),
  budget_reset_at: z.iso.datetime({ offset: true }).nullable(),
};
const summarySchema = z.object({ organizations: z.array(z.object(organizationFields)) });

const dollars = new Intl.NumberFormat("ru-RU", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const moscowTimeOptions: Intl.DateTimeFormatOptions = {
  timeZone: "Europe/Moscow",
  day: "2-digit",
  month: "2-digit",
  year: "numeric",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hourCycle: "h23",
};
const moscowTime = new Intl.DateTimeFormat("ru-RU", moscowTimeOptions);
const client = createApiClient({ getBaseUrl: getProxyBaseUrl, getAuthHeaderName: getGlobalLitellmHeaderName });

interface Props {
  accessToken: string | null;
  userId: string | null;
}

export default function OrganizationBudgetCard({ accessToken, userId }: Props) {
  const options = {
    queryKey: ["organization-budget-summary", userId, accessToken],
    queryFn: async ({ signal }: { signal: AbortSignal }) => {
      const response = await client.get<unknown>("/organization/budget-summary", { accessToken, signal });
      return summarySchema.parse(response);
    },
    enabled: Boolean(accessToken && userId),
    gcTime: 0,
    staleTime: 0,
    retry: false,
    refetchOnMount: "always" as const,
    refetchOnWindowFocus: "always" as const,
    refetchInterval: 60_000,
    placeholderData: undefined,
  };
  const summary = useQuery(options);

  if (!accessToken || !userId) return null;
  if (summary.isPending) {
    return (
      <div role="status" aria-live="polite">
        <span className="text-sm text-muted-foreground">Загрузка бюджета организации</span>
        <Skeleton className="mt-2 h-24 w-full" />
      </div>
    );
  }
  if (summary.isError) {
    return (
      <p role="alert" className="text-sm text-destructive">
        Не удалось загрузить бюджет организации
      </p>
    );
  }
  if (!summary.data.organizations.length) return null;

  return (
    <div className="grid gap-4">
      {summary.data.organizations.map((organization) => (
        <Card key={organization.organization_id} size="sm">
          <CardHeader>
            <CardTitle>
              <h2>Бюджет организации</h2>
            </CardTitle>
            <p className="text-sm text-muted-foreground">
              {organization.organization_alias || organization.organization_id}
            </p>
          </CardHeader>
          <CardContent className="space-y-4">
            <dl className="grid gap-4 sm:grid-cols-3">
              <div>
                <dt className="text-muted-foreground">
                  {organization.budget_duration === "1mo" && "Месячный лимит"}
                  {organization.budget_duration !== "1mo" &&
                    `Лимит${organization.budget_duration ? ` (${organization.budget_duration})` : ""}`}
                </dt>
                <dd className="font-medium tabular-nums">
                  {organization.max_budget === null ? "Без лимита" : `${dollars.format(organization.max_budget)} USD`}
                </dd>
              </div>
              <div>
                <dt className="text-muted-foreground">Потрачено</dt>
                <dd className="font-medium tabular-nums">{dollars.format(organization.spend)} USD</dd>
              </div>
              <div>
                <dt className="text-muted-foreground">Осталось</dt>
                <dd className="font-medium tabular-nums">
                  {organization.remaining_budget === null
                    ? "Без лимита"
                    : `${dollars.format(organization.remaining_budget)} USD`}
                </dd>
              </div>
            </dl>
            {organization.max_budget !== null && organization.spend >= organization.max_budget && (
              <p className="text-sm text-destructive">Запросы заблокированы: бюджет исчерпан</p>
            )}
            <div className="text-sm text-muted-foreground">
              {organization.budget_reset_at ? (
                <>
                  Бюджет обновится:{" "}
                  <time dateTime={organization.budget_reset_at}>
                    {moscowTime.format(new Date(organization.budget_reset_at))} МСК
                  </time>
                </>
              ) : (
                "Автоматический сброс не настроен"
              )}
            </div>
          </CardContent>
        </Card>
      ))}
    </div>
  );
}
