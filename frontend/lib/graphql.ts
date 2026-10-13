export class GraphQLRequestError extends Error {
  constructor(
    message: string,
    public readonly code?: string,
  ) {
    super(message);
  }
}

type GraphQLResponse<T> = {
  data?: T;
  errors?: { message: string; extensions?: { code?: string } }[];
};

export async function gql<T>(
  query: string,
  variables: Record<string, unknown> = {},
  options: { adminToken?: string } = {},
): Promise<T> {
  const headers: Record<string, string> = { "content-type": "application/json" };
  if (options.adminToken) headers["x-admin-token"] = options.adminToken;

  let response: Response;
  try {
    response = await fetch("/graphql", {
      method: "POST",
      headers,
      body: JSON.stringify({ query, variables }),
    });
  } catch {
    throw new GraphQLRequestError("Can't reach the shop API. Check your connection and try again.", "NETWORK");
  }

  let body: GraphQLResponse<T>;
  try {
    body = await response.json();
  } catch {
    throw new GraphQLRequestError(`The shop API returned HTTP ${response.status}.`, "HTTP");
  }
  if (body.errors?.length) {
    const [first] = body.errors;
    throw new GraphQLRequestError(first.message, first.extensions?.code);
  }
  if (!body.data) throw new GraphQLRequestError("Empty response from the shop API.");
  return body.data;
}

export function newRequestId(): string {
  return crypto.randomUUID();
}
