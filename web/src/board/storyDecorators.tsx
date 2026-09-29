// Storybook decorator for board pieces that link into the app (`/c/…`,
// `/inbox`) or load a session's sub-agents.

import type { Decorator } from "@storybook/react-vite";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";

const storyQueryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });

export const withBoardProviders: Decorator = (Story) => (
  <QueryClientProvider client={storyQueryClient}>
    <MemoryRouter initialEntries={["/board"]}>
      <Story />
    </MemoryRouter>
  </QueryClientProvider>
);
