import { LIVE_SCREENS, deskNav } from "./navigation";

export function DeskNav({ active, onSelect }: { active: string; onSelect: (x: string) => void }) {
  return <aside className="desk-nav">
    <div className="desk-title">CRYPTO DESK <span>INSTITUTIONAL DIGITAL ASSETS</span></div>
    {deskNav.map(g => <div className="desk-group" key={g.group}><label>{g.group}</label>
      {g.items.map(i => {
        const live = LIVE_SCREENS.has(i);
        return <button className={active === i ? "active" : ""} key={i}
          onClick={() => onSelect(i)}
          title={live ? "Live data" : "Phase 1 fixture data"}>
          {i}
          {/* A marker per item, so the boundary between measured and
              illustrative is visible before the screen is opened. */}
          <span className={`prov ${live ? "live" : "sim"}`}>{live ? "LIVE" : "SIM"}</span>
        </button>;
      })}
    </div>)}
    <div className="desk-legend">
      <span><i className="prov-dot live" /> live venue / chain / engine data</span>
      <span><i className="prov-dot sim" /> Phase 1 fixtures</span>
    </div>
  </aside>;
}
