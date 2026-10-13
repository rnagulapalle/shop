"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState } from "react";
import { gql } from "@/lib/graphql";
import { money, Order, ORDERS_QUERY, STATUS_LABEL } from "@/lib/shop";

function OrdersLookup() {
  const router = useRouter();
  const params = useSearchParams();
  const email = params.get("email") ?? "";
  const [input, setInput] = useState(email);
  const [orders, setOrders] = useState<Order[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!email) return;
    setError(null);
    gql<{ orders: Order[] }>(ORDERS_QUERY, { email })
      .then((d) => setOrders(d.orders))
      .catch((e: Error) => setError(e.message));
  }, [email]);

  return (
    <div className="stack">
      <h1>My orders</h1>
      <form
        className="row"
        style={{ justifyContent: "flex-start" }}
        onSubmit={(e) => {
          e.preventDefault();
          router.push(`/orders?email=${encodeURIComponent(input.trim())}`);
        }}
      >
        <input type="email" required value={input} onChange={(e) => setInput(e.target.value)}
          placeholder="Email used at checkout" style={{ maxWidth: 320 }} />
        <button type="submit">Find orders</button>
      </form>
      {error && <div className="alert error" role="alert">{error}</div>}
      {orders && orders.length === 0 && <p className="muted">No orders for {email}.</p>}
      {orders?.map((o) => (
        <Link key={o.id} href={`/orders/${o.id}?email=${encodeURIComponent(o.email)}`}
          className="card row" style={{ textDecoration: "none", color: "inherit" }}>
          <div>
            <strong>{o.id}</strong>
            <div className="muted">{new Date(o.createdAt).toLocaleString()} · {o.items.length} item(s)</div>
          </div>
          <div className="row">
            <span className={`badge ${o.status}`}>{STATUS_LABEL[o.status]}</span>
            <strong>{money(o.totalCents, o.currency)}</strong>
          </div>
        </Link>
      ))}
    </div>
  );
}

export default function OrdersPage() {
  return (
    <Suspense>
      <OrdersLookup />
    </Suspense>
  );
}
