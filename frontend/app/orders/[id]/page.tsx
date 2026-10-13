"use client";

import Link from "next/link";
import { useParams, useSearchParams } from "next/navigation";
import { Suspense, useCallback, useEffect, useState } from "react";
import { gql, newRequestId } from "@/lib/graphql";
import {
  CANCEL_ORDER,
  FULFILL_ORDER,
  money,
  Order,
  ORDER_QUERY,
  REFUND_ORDER,
  STATUS_LABEL,
} from "@/lib/shop";

const EMAIL_LABEL: Record<string, string> = {
  order_confirmed: "Order confirmation",
  order_canceled: "Cancellation notice",
  order_shipped: "Shipping notice",
  refund_issued: "Refund receipt",
};

function OrderDetail() {
  const { id } = useParams<{ id: string }>();
  const email = useSearchParams().get("email") ?? "";
  const [order, setOrder] = useState<Order | null>(null);
  const [notFound, setNotFound] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const { order } = await gql<{ order: Order | null }>(ORDER_QUERY, { id });
      setOrder(order);
      setNotFound(!order);
    } catch (e) {
      setError((e as Error).message);
    }
  }, [id]);

  useEffect(() => {
    load();
  }, [load]);

  async function run(label: string, fn: () => Promise<Order>, success: string) {
    setBusy(label);
    setError(null);
    setNotice(null);
    try {
      setOrder(await fn());
      setNotice(success);
    } catch (e) {
      setError((e as Error).message);
      await load();
    } finally {
      setBusy(null);
    }
  }

  if (notFound) return <p>Order not found. <Link href="/orders">Find your orders</Link></p>;
  if (!order) return error ? <div className="alert error">{error}</div> : <p className="muted">Loading…</p>;

  const cancel = () => {
    if (!confirm("Cancel this order? The hold on your card will be released.")) return;
    run("cancel", async () =>
      (await gql<{ cancelOrder: Order }>(CANCEL_ORDER, { id: order.id, email: email || order.email })).cancelOrder,
      "Order cancelled. You have not been charged.");
  };

  return (
    <div className="stack">
      <div className="row">
        <h1 style={{ margin: 0 }}>Order {order.id}</h1>
        <span className={`badge ${order.status}`}>{STATUS_LABEL[order.status]}</span>
      </div>
      <p className="muted" style={{ margin: 0 }}>
        Placed {new Date(order.createdAt).toLocaleString()} by {order.email}
      </p>

      {order.lastError && <div className="alert error">{order.lastError}</div>}
      {error && <div className="alert error" role="alert">{error}</div>}
      {notice && <div className="alert ok" role="status">{notice}</div>}

      <div className="card">
        <table>
          <tbody>
            {order.items.map((i) => (
              <tr key={i.productId}>
                <td>{i.quantity} × {i.name}</td>
                <td className="num">{money(i.unitPriceCents * i.quantity, order.currency)}</td>
              </tr>
            ))}
            <tr><th>Total</th><th className="num">{money(order.totalCents, order.currency)}</th></tr>
            <tr><td className="muted">Charged</td><td className="num">{money(order.capturedCents, order.currency)}</td></tr>
            {order.refundedCents > 0 && (
              <tr><td className="muted">Refunded</td><td className="num">−{money(order.refundedCents, order.currency)}</td></tr>
            )}
          </tbody>
        </table>
      </div>

      {order.canCancel && (
        <div className="row">
          <span className="muted">Changed your mind? You can cancel until the order ships.</span>
          <button className="danger" onClick={cancel} disabled={!!busy}>
            {busy === "cancel" ? "Cancelling…" : "Cancel order"}
          </button>
        </div>
      )}

      {order.refunds.length > 0 && (
        <>
          <h2>Refunds</h2>
          <div className="card">
            <table>
              <tbody>
                {order.refunds.map((r) => (
                  <tr key={r.id}>
                    <td>{new Date(r.createdAt).toLocaleString()}{r.reason ? ` · ${r.reason}` : ""}</td>
                    <td>{r.status.toLowerCase()}{r.lastError ? ` (${r.lastError})` : ""}</td>
                    <td className="num">{money(r.amountCents, order.currency)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}

      {order.emails.length > 0 && (
        <>
          <h2>Emails</h2>
          <ul className="muted" style={{ margin: 0 }}>
            {order.emails.map((e) => (
              <li key={e.kind + e.createdAt}>
                {EMAIL_LABEL[e.kind] ?? e.kind}: {e.status.toLowerCase()}{e.lastError ? ` (${e.lastError})` : ""}
              </li>
            ))}
          </ul>
        </>
      )}

      <StaffPanel order={order} busy={busy} run={run} />
    </div>
  );
}

function StaffPanel({
  order,
  busy,
  run,
}: {
  order: Order;
  busy: string | null;
  run: (label: string, fn: () => Promise<Order>, success: string) => Promise<void>;
}) {
  const [token, setToken] = useState("");
  const [amount, setAmount] = useState("");
  const [reason, setReason] = useState("");
  // Kept across retries of the same refund so it is never issued twice.
  const [refundRequestId, setRefundRequestId] = useState(newRequestId);

  useEffect(() => setRefundRequestId(newRequestId()), [amount, reason]);

  const canShip = order.status === "AUTHORIZED";
  const canRefund = order.refundableCents > 0;
  if (!canShip && !canRefund) return null;

  const ship = () =>
    run("ship", async () =>
      (await gql<{ fulfillOrder: Order }>(FULFILL_ORDER, { id: order.id }, { adminToken: token })).fulfillOrder,
      "Order shipped and payment captured.");

  const refund = (e: React.FormEvent) => {
    e.preventDefault();
    const cents = amount.trim() ? Math.round(parseFloat(amount) * 100) : null;
    run("refund", async () => {
      const result = (await gql<{ refundOrder: Order }>(
        REFUND_ORDER,
        { input: { orderId: order.id, requestId: refundRequestId, amountCents: cents, reason: reason || null } },
        { adminToken: token },
      )).refundOrder;
      setAmount("");
      setReason("");
      setRefundRequestId(newRequestId());
      return result;
    }, "Refund issued.");
  };

  return (
    <>
      <h2>Staff actions</h2>
      <div className="card stack">
        <label>
          Staff token
          <input type="password" value={token} onChange={(e) => setToken(e.target.value)} placeholder="ADMIN_TOKEN" />
        </label>
        {canShip && (
          <div className="row">
            <span className="muted">Ship the order and capture {money(order.totalCents, order.currency)}.</span>
            <button className="primary" onClick={ship} disabled={!!busy}>
              {busy === "ship" ? "Shipping…" : "Mark shipped"}
            </button>
          </div>
        )}
        {canRefund && (
          <form className="stack" onSubmit={refund}>
            <div className="row" style={{ alignItems: "end" }}>
              <label style={{ flex: 1 }}>
                Amount (blank = full {money(order.refundableCents, order.currency)})
                <input inputMode="decimal" value={amount} onChange={(e) => setAmount(e.target.value)} placeholder="0.00" />
              </label>
              <label style={{ flex: 2 }}>
                Reason (optional)
                <input value={reason} onChange={(e) => setReason(e.target.value)} />
              </label>
            </div>
            <button type="submit" disabled={!!busy}>{busy === "refund" ? "Refunding…" : "Issue refund"}</button>
          </form>
        )}
      </div>
    </>
  );
}

export default function OrderPage() {
  return (
    <Suspense>
      <OrderDetail />
    </Suspense>
  );
}
