import { ApiError } from "./api";

export function Icon({ name, size = 16 }) {
  const paths = {
    check: "M3.5 8.5l3 3 6-7",
    cross: "M4 4l8 8M12 4l-8 8",
    mic: "M8 2.5a2 2 0 0 0-2 2v3.5a2 2 0 0 0 4 0V4.5a2 2 0 0 0-2-2zM4 8a4 4 0 0 0 8 0M8 12v2",
    stop: "M4.5 4.5h7v7h-7z",
    send: "M2.5 8l11-5-4 11-2-4.5z",
    refresh: "M13 8a5 5 0 1 1-1.6-3.7M13 2.5v3h-3",
    link: "M7 9l2-2M6 6.5L4.5 8a2.1 2.1 0 0 0 3 3L9 9.5M10 9.5l1.5-1.5a2.1 2.1 0 0 0-3-3L7 6.5",
  };
  return (
    <svg viewBox="0 0 16 16" width={size} height={size} fill="none" stroke="currentColor"
         strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false">
      <path d={paths[name]} />
    </svg>
  );
}

// A chip carries state only. Person (amber) means someone has to act; instrument (cool) means
// the system measured or did it; ok and bad are for results.
export function Chip({ tone = "quiet", children }) {
  return <span className={`chip chip--${tone}`}>{children}</span>;
}

export function Button({ variant = "secondary", busy, children, ...rest }) {
  return (
    <button type="button" className={`btn btn--${variant}`} aria-busy={busy || undefined}
            disabled={rest.disabled || busy} {...rest}>
      {children}
    </button>
  );
}

// A value at display size in the measured face, with who, when or why beneath it.
export function Reading({ value, caption, children }) {
  return (
    <div className="reading-block">
      <p className="reading">{value}</p>
      {caption && <p className="reading__caption">{caption}</p>}
      {children}
    </div>
  );
}

export function Skeleton({ rows = 5, tall }) {
  return (
    <div className="skeleton" aria-hidden="true">
      {Array.from({ length: rows }, (_, i) => (
        <div key={i} className={`skeleton__row${tall ? " skeleton__row--tall" : ""}`} />
      ))}
    </div>
  );
}

function errorCopy(error) {
  if (error instanceof ApiError && error.status === 0) {
    return {
      title: "Cannot reach the Sahayak server",
      body: "Start it with INPROCESS_MOCK=1 uvicorn backend.main:app --port 8000, then try again.",
    };
  }
  return { title: "Something went wrong", body: error?.message || "The request failed. Try again." };
}

// Every region that loads data draws four states: loading, empty, error and full.
export function Region({ query, isEmpty, empty, skeleton, children }) {
  const { data, error, loading, reload } = query;
  if (loading && data === null) return skeleton || <Skeleton />;
  if (error && data === null) {
    const copy = errorCopy(error);
    return (
      <div className="state" role="alert">
        <h3>{copy.title}</h3>
        <p>{copy.body}</p>
        <Button onClick={reload}><Icon name="refresh" /> Try again</Button>
      </div>
    );
  }
  if (data !== null && (isEmpty ? isEmpty(data) : Array.isArray(data) && data.length === 0)) {
    return (
      <div className="state">
        <h3>{empty.title}</h3>
        <p>{empty.body}</p>
        {empty.action}
      </div>
    );
  }
  return (
    <>
      {error && (
        <p className="stale" role="status">Showing the last data received. {error.message}</p>
      )}
      {children(data)}
    </>
  );
}
