import { Link } from "react-router-dom";
import { api } from "../api";
import { usePoll, useMerchants } from "../hooks";
import { Chip, Region, Skeleton } from "../ui";

// Every rep, the merchants it looks after, and what it is doing right now.
export default function Team() {
  const query = usePoll(() => api.agents(), [], 2000);
  const merchants = useMerchants();

  return (
    <div className="page">
      <div className="toolbar"><h1>Team</h1></div>
      <Region
        query={query}
        skeleton={<Skeleton rows={6} />}
        empty={{ title: "No reps set up", body: "Add reps and their merchants in agents.yaml." }}
      >
        {(agents) => (
          <div className="stack">
            {agents.map((a) => (
              <section key={a.id} aria-labelledby={`rep-${a.id}`}>
                <h2 className="section" id={`rep-${a.id}`}>{a.name}</h2>
                <dl className="stats">
                  <div><dt>Working now</dt><dd className="measured">{a.working} of {a.concurrency}</dd></div>
                  <div><dt>Waiting on someone</dt><dd className="measured">{a.waiting}</dd></div>
                  <div><dt>Finished</dt><dd className="measured">{a.finished}</dd></div>
                  <div><dt>Handed to a person</dt><dd className="measured">{a.handed_to_person}</dd></div>
                </dl>
                <table className="table table--stack">
                  <thead><tr><th>Merchant</th><th>Business</th><th>Now</th></tr></thead>
                  <tbody>
                    {a.merchants.map((id) => {
                      const m = merchants.byId[id];
                      const busy = a.active_merchants.includes(id);
                      return (
                        <tr key={id}>
                          <td data-label="Merchant">
                            <Link className="rowlink" to={`/cases?merchant=${id}`}>{m?.owner_name || id}</Link>
                            <span className="sub"><span className="measured">{id}</span></span>
                          </td>
                          <td data-label="Business">{m?.business_name || ""}</td>
                          <td data-label="Now"><Chip tone={busy ? "ok" : "quiet"}>{busy ? "In a case" : "Idle"}</Chip></td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
                <p className="sub"><Link to={`/cases?agent=${a.id}`}>All of {a.name}’s cases</Link></p>
              </section>
            ))}
          </div>
        )}
      </Region>
    </div>
  );
}
