import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api";

// Fetch on mount and again every `ms`. Keeps the last good data while refreshing, and reports
// `loading` only before the first answer, so a table never flashes back to a skeleton.
export function usePoll(fetcher, deps = [], ms = 2000) {
  const [state, setState] = useState({ data: null, error: null, loading: true });
  const fetchRef = useRef(fetcher);
  fetchRef.current = fetcher;

  const load = useCallback(async () => {
    try {
      const data = await fetchRef.current();
      setState({ data, error: null, loading: false });
    } catch (error) {
      setState((s) => ({ data: s.data, error, loading: false }));
    }
  }, []);

  useEffect(() => {
    setState((s) => ({ ...s, loading: s.data === null }));
    let stopped = false;
    const tick = () => {
      if (!stopped && !document.hidden) load();
    };
    tick();
    const id = setInterval(tick, ms);
    return () => {
      stopped = true;
      clearInterval(id);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [load, ms, ...deps]);

  return { ...state, reload: load };
}

// The live trail for one case. The server replays everything so far, then streams new events,
// and sends an `end` event when the case has finished.
export function useCaseEvents(threadId) {
  const [events, setEvents] = useState([]);
  const [live, setLive] = useState(false);

  useEffect(() => {
    setEvents([]);
    if (!threadId) return undefined;
    const source = api.events(threadId);
    setLive(true);
    source.addEventListener("audit", (e) => {
      const event = JSON.parse(e.data);
      setEvents((prev) => (prev.some((p) => p.seq === event.seq) ? prev : [...prev, event]));
    });
    source.addEventListener("end", () => {
      setLive(false);
      source.close();
    });
    source.onerror = () => setLive(false);
    return () => source.close();
  }, [threadId]);

  return { events, live };
}

export function useMerchants() {
  const { data } = usePoll(() => api.merchants(), [], 60000);
  const byId = Object.fromEntries((data || []).map((m) => [m.id, m]));
  return { list: data || [], byId };
}
