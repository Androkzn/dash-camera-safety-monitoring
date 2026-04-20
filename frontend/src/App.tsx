/**
 * App — root React component. Just delegates to the lazy router.
 * The route table lives in `app/router.tsx`.
 */
import { AppRouter } from "./app/router";

export function App() {
  return <AppRouter />;
}
