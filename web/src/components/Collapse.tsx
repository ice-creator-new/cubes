/** A wrapper that folds its contents in and out; the contents may outlive `open` by a beat. */
export function Collapse({ open, children }: { open: boolean; children?: React.ReactNode }) {
  return (
    <div className={open ? "collapse open" : "collapse"} inert={!open}>
      <div className="collapse-inner">{children}</div>
    </div>
  );
}
