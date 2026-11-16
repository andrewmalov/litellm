import { act, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider, focusManager } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import OrganizationBudgetCard from "./OrganizationBudgetCard";

vi.mock("@/components/networking", () => ({
  getProxyBaseUrl: () => "",
  getGlobalLitellmHeaderName: () => "Authorization",
}));

const organization = {
  organization_id: "org-a",
  organization_alias: "Тестовая организация",
  max_budget: 1000,
  spend: 125.5,
  remaining_budget: 874.5,
  budget_duration: "1mo",
  budget_reset_day: 16,
  budget_reset_at: "2030-12-15T21:00:00Z",
};

const fetchMock = vi.fn<typeof fetch>();
const response = (organizations = [organization]) => Response.json({ organizations });

function renderCard(accessToken = "member-session", userId = "member") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = render(
    <QueryClientProvider client={client}>
      <OrganizationBudgetCard accessToken={accessToken} userId={userId} />
    </QueryClientProvider>,
  );
  return { ...view, client };
}

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  focusManager.setFocused(undefined);
});

describe("organization budget for a dashboard member", () => {
  it("uses the member session and displays server budget and Moscow reset", async () => {
    fetchMock.mockResolvedValue(response());
    renderCard();
    expect(await screen.findByText("Тестовая организация")).toBeInTheDocument();
    expect(screen.getByText("Месячный лимит")).toBeInTheDocument();
    expect(screen.getByText("125,50 USD")).toBeInTheDocument();
    expect(screen.getByText("874,50 USD")).toBeInTheDocument();
    expect(screen.getByText(/Бюджет обновится:/)).toBeInTheDocument();
    expect(screen.getByText("16.12.2030, 00:00:00 МСК")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith(
      "/organization/budget-summary",
      expect.objectContaining({
        headers: expect.objectContaining({ Authorization: "Bearer member-session" }),
      }),
    );
  });

  it("shows blocking at zero and actual overspend", async () => {
    fetchMock.mockResolvedValue(response([{ ...organization, max_budget: 0, spend: 1, remaining_budget: 0 }]));
    renderCard();
    expect(await screen.findByText("Запросы заблокированы: бюджет исчерпан")).toBeInTheDocument();
    expect(screen.getByText("1,00 USD")).toBeInTheDocument();
    expect(screen.getAllByText("0,00 USD")).toHaveLength(2);
  });

  it("preserves unlimited and disabled schedule states without a monthly label", async () => {
    fetchMock.mockResolvedValue(
      Response.json({
        organizations: [
          {
            ...organization,
            max_budget: null,
            remaining_budget: null,
            budget_duration: null,
            budget_reset_at: null,
          },
        ],
      }),
    );
    renderCard();
    expect(await screen.findByText("Автоматический сброс не настроен")).toBeInTheDocument();
    expect(screen.getAllByText("Без лимита")).toHaveLength(2);
    expect(screen.queryByText("Месячный лимит")).not.toBeInTheDocument();
    expect(screen.queryByText(/Запросы заблокированы/)).not.toBeInTheDocument();
  });

  it("does not call another budget period monthly", async () => {
    fetchMock.mockResolvedValue(response([{ ...organization, budget_duration: "7d" }]));
    renderCard();
    expect(await screen.findByText("Лимит (7d)")).toBeInTheDocument();
    expect(screen.queryByText("Месячный лимит")).not.toBeInTheDocument();
  });

  it("shows loading and an API error instead of a zero balance", async () => {
    let fail: (reason: Error) => void = () => {};
    fetchMock.mockImplementation(
      () =>
        new Promise((_resolve, reject) => {
          fail = reject;
        }),
    );
    renderCard();
    expect(screen.getByRole("status")).toHaveTextContent("Загрузка бюджета организации");
    await act(async () => fail(new Error("unavailable")));
    expect(await screen.findByRole("alert")).toHaveTextContent("Не удалось загрузить бюджет организации");
    expect(screen.queryByText("0,00 USD")).not.toBeInTheDocument();
  });

  it("hides previous member data immediately on session change", async () => {
    fetchMock.mockResolvedValueOnce(response()).mockImplementation(() => new Promise(() => {}));
    const { rerender, client } = renderCard();
    expect(await screen.findByText("Тестовая организация")).toBeInTheDocument();
    rerender(
      <QueryClientProvider client={client}>
        <OrganizationBudgetCard accessToken="other-session" userId="other-member" />
      </QueryClientProvider>,
    );
    expect(screen.queryByText("Тестовая организация")).not.toBeInTheDocument();
    expect(screen.getByRole("status")).toBeInTheDocument();
  });

  it("refreshes on return and removes a revoked membership", async () => {
    fetchMock.mockResolvedValueOnce(response()).mockResolvedValue(response([]));
    renderCard();
    expect(await screen.findByText("Тестовая организация")).toBeInTheDocument();
    await act(async () => {
      focusManager.setFocused(false);
      focusManager.setFocused(true);
    });
    await waitFor(() => expect(screen.queryByText("Тестовая организация")).not.toBeInTheDocument());
    expect(screen.queryByText("Бюджет организации")).not.toBeInTheDocument();
  });

  it("refreshes every minute and surfaces a refresh error", async () => {
    fetchMock.mockResolvedValueOnce(response()).mockResolvedValue(new Response(null, { status: 503 }));
    vi.useFakeTimers();
    renderCard();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10);
    });
    expect(screen.getByText("Тестовая организация")).toBeInTheDocument();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10);
    });
    expect(screen.getByRole("alert")).toHaveTextContent("Не удалось загрузить бюджет организации");
    expect(screen.queryByText("874,50 USD")).not.toBeInTheDocument();
  });
});
