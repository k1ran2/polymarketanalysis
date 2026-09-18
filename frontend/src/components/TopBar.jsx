// Deliberately minimal for now -- a bot-status chip and kill switch belong
// here once the live trading system exists (see the project's build plan),
// not before: a status pill for a bot that can't run yet would just be
// decoration pretending to be information.
export default function TopBar() {
  return (
    <div className="topbar">
      <span className="bot-status-chip">
        <span className="dot" />
        Read-only research tool — no live trading connected
      </span>
    </div>
  );
}
