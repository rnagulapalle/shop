export type OrderStatus =
  | "PENDING"
  | "AUTHORIZED"
  | "PAYMENT_FAILED"
  | "CANCELED"
  | "FULFILLED"
  | "PARTIALLY_REFUNDED"
  | "REFUNDED";

export type Product = {
  id: string;
  name: string;
  description: string;
  priceCents: number;
  image: string;
};

export type Order = {
  id: string;
  email: string;
  status: OrderStatus;
  currency: string;
  totalCents: number;
  capturedCents: number;
  refundedCents: number;
  refundableCents: number;
  canCancel: boolean;
  lastError: string | null;
  createdAt: string;
  items: { productId: string; name: string; unitPriceCents: number; quantity: number }[];
  refunds: { id: string; amountCents: number; reason: string | null; status: string; lastError: string | null; createdAt: string }[];
  emails: { kind: string; status: string; lastError: string | null; createdAt: string }[];
};

const ORDER_FIELDS = `
  id email status currency totalCents capturedCents refundedCents refundableCents
  canCancel lastError createdAt
  items { productId name unitPriceCents quantity }
  refunds { id amountCents reason status lastError createdAt }
  emails { kind status lastError createdAt }
`;

export const PRODUCTS_QUERY = `query { products { id name description priceCents image } }`;
export const ORDER_QUERY = `query($id: ID!) { order(id: $id) { ${ORDER_FIELDS} } }`;
export const ORDERS_QUERY = `query($email: String!) { orders(email: $email) { ${ORDER_FIELDS} } }`;
export const PLACE_ORDER = `mutation($input: PlaceOrderInput!) { placeOrder(input: $input) { ${ORDER_FIELDS} } }`;
export const CANCEL_ORDER = `mutation($id: ID!, $email: String!) { cancelOrder(orderId: $id, email: $email) { ${ORDER_FIELDS} } }`;
export const FULFILL_ORDER = `mutation($id: ID!) { fulfillOrder(orderId: $id) { ${ORDER_FIELDS} } }`;
export const REFUND_ORDER = `mutation($input: RefundOrderInput!) { refundOrder(input: $input) { ${ORDER_FIELDS} } }`;

// Stripe test-mode PaymentMethods. A production checkout would collect the
// card with Stripe Elements and pass the resulting PaymentMethod id instead.
export const TEST_CARDS = [
  { id: "pm_card_visa", label: "Visa 4242 (succeeds)" },
  { id: "pm_card_chargeDeclined", label: "Card declined" },
  { id: "pm_card_chargeDeclinedInsufficientFunds", label: "Insufficient funds" },
];

export function money(cents: number, currency = "usd"): string {
  return new Intl.NumberFormat("en-US", { style: "currency", currency: currency.toUpperCase() }).format(cents / 100);
}

export const STATUS_LABEL: Record<OrderStatus, string> = {
  PENDING: "Payment pending",
  AUTHORIZED: "Confirmed",
  PAYMENT_FAILED: "Payment failed",
  CANCELED: "Cancelled",
  FULFILLED: "Shipped",
  PARTIALLY_REFUNDED: "Partially refunded",
  REFUNDED: "Refunded",
};
