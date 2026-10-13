"use client";

import { useRouter } from "next/navigation";
import { useEffect, useMemo, useState } from "react";
import { gql, newRequestId } from "@/lib/graphql";
import { money, Order, PLACE_ORDER, Product, PRODUCTS_QUERY, TEST_CARDS } from "@/lib/shop";

export default function CatalogPage() {
  const router = useRouter();
  const [products, setProducts] = useState<Product[]>([]);
  const [cart, setCart] = useState<Record<string, number>>({});
  const [email, setEmail] = useState("");
  const [paymentMethod, setPaymentMethod] = useState(TEST_CARDS[0].id);
  // One id per checkout attempt: resubmitting after a timeout reuses it, so
  // the backend never creates (or charges) a second order.
  const [checkoutId, setCheckoutId] = useState("");
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    gql<{ products: Product[] }>(PRODUCTS_QUERY)
      .then((d) => setProducts(d.products))
      .catch((e: Error) => setError(e.message))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => setCheckoutId(newRequestId()), [cart, email, paymentMethod]);

  const lines = useMemo(
    () => products.filter((p) => cart[p.id]).map((p) => ({ product: p, quantity: cart[p.id] })),
    [products, cart],
  );
  const total = lines.reduce((sum, l) => sum + l.product.priceCents * l.quantity, 0);

  function setQty(id: string, qty: number) {
    setCart((c) => {
      const next = { ...c };
      if (qty <= 0) delete next[id];
      else next[id] = Math.min(qty, 99);
      return next;
    });
  }

  async function checkout(e: React.FormEvent) {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      const { placeOrder } = await gql<{ placeOrder: Order }>(PLACE_ORDER, {
        input: {
          checkoutId,
          email,
          paymentMethod,
          items: lines.map((l) => ({ productId: l.product.id, quantity: l.quantity })),
        },
      });
      if (placeOrder.status === "AUTHORIZED") {
        setCart({});
        router.push(`/orders/${placeOrder.id}?email=${encodeURIComponent(placeOrder.email)}`);
        return;
      }
      // PAYMENT_FAILED: start a fresh attempt. PENDING: keep the id so a retry is safe.
      if (placeOrder.status === "PAYMENT_FAILED") setCheckoutId(newRequestId());
      setError(placeOrder.lastError ?? `Order is ${placeOrder.status.toLowerCase()}.`);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="layout">
      <section>
        <h1>Products</h1>
        {loading && <p className="muted">Loading…</p>}
        <div className="grid">
          {products.map((p) => (
            <div key={p.id} className="card product">
              <div className="emoji" aria-hidden>{p.image}</div>
              <h3>{p.name}</h3>
              <p className="muted">{p.description}</p>
              <div className="row">
                <strong>{money(p.priceCents)}</strong>
                {cart[p.id] ? (
                  <div className="qty">
                    <button onClick={() => setQty(p.id, cart[p.id] - 1)} aria-label={`Remove one ${p.name}`}>−</button>
                    <span>{cart[p.id]}</span>
                    <button onClick={() => setQty(p.id, cart[p.id] + 1)} aria-label={`Add one ${p.name}`}>+</button>
                  </div>
                ) : (
                  <button onClick={() => setQty(p.id, 1)}>Add</button>
                )}
              </div>
            </div>
          ))}
        </div>
      </section>

      <aside className="card">
        <h2 style={{ marginTop: 0 }}>Your cart</h2>
        {lines.length === 0 ? (
          <p className="muted">Your cart is empty.</p>
        ) : (
          <form className="stack" onSubmit={checkout}>
            <table>
              <tbody>
                {lines.map((l) => (
                  <tr key={l.product.id}>
                    <td>{l.quantity} × {l.product.name}</td>
                    <td className="num">{money(l.product.priceCents * l.quantity)}</td>
                  </tr>
                ))}
                <tr>
                  <th>Total</th>
                  <th className="num">{money(total)}</th>
                </tr>
              </tbody>
            </table>
            <label>
              Email
              <input type="email" required value={email} onChange={(e) => setEmail(e.target.value)} placeholder="you@example.com" />
            </label>
            <label>
              Card (Stripe test mode)
              <select value={paymentMethod} onChange={(e) => setPaymentMethod(e.target.value)}>
                {TEST_CARDS.map((c) => (
                  <option key={c.id} value={c.id}>{c.label}</option>
                ))}
              </select>
            </label>
            <p className="muted" style={{ margin: 0, fontSize: 13 }}>
              Your card is authorised now and only charged when the order ships.
            </p>
            {error && <div className="alert error" role="alert">{error}</div>}
            <button className="primary" type="submit" disabled={submitting || !checkoutId}>
              {submitting ? "Placing order…" : `Place order · ${money(total)}`}
            </button>
          </form>
        )}
      </aside>
    </div>
  );
}
