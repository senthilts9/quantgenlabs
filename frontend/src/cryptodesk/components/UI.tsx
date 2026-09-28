import { useEffect } from "react";
import type { ReactNode } from "react";
import type { Status } from "../types/domain";

export function Panel({title, right, children, className=""}:{title?:string;right?:ReactNode;children:ReactNode;className?:string}) {
  return <section className={`panel ${className}`}>
    {(title || right) && <header className="panel-head"><div>{title}</div><div>{right}</div></header>}
    <div className="panel-body">{children}</div>
  </section>
}
export function StatusPill({status}:{status:Status|string}) {
  const cls = String(status).toLowerCase().replaceAll(" ","-");
  return <span className={`status ${cls}`}><i />{status}</span>;
}
export function Metric({label,value,sub,tone=""}:{label:string;value:string;sub?:string;tone?:string}) {
  return <div className="metric"><span>{label}</span><strong className={tone}>{value}</strong>{sub && <small>{sub}</small>}</div>;
}
export function Tabs({items,active,onChange}:{items:string[];active:string;onChange:(x:string)=>void}) {
  return <div className="tabs">{items.map(x=><button key={x} className={active===x?"active":""} onClick={()=>onChange(x)}>{x}</button>)}</div>;
}
export function Empty({text}:{text:string}) { return <div className="empty">{text}</div>; }

/**
 * Modal shell. Click-outside and Escape both close, because a dialog that can
 * only be dismissed by finding its button is a dialog an operator learns to
 * avoid opening.
 */
export function Modal({title, onClose, children, footer}:{title:string;onClose:()=>void;children:ReactNode;footer?:ReactNode}) {
  // Escape is bound on the document, not on the backdrop: a div receives key
  // events only while focused, and nothing focuses the backdrop, so the
  // obvious `onKeyDown` handler never fires.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  return <div className="modal-backdrop" onClick={onClose} role="presentation">
    <div className="modal-card" onClick={e => e.stopPropagation()} role="dialog" aria-label={title}>
      <header className="panel-head"><div>{title}</div>
        <button className="mini" onClick={onClose} aria-label="Close">✕</button></header>
      <div className="modal-body">{children}</div>
      {footer && <footer className="modal-foot">{footer}</footer>}
    </div>
  </div>;
}
