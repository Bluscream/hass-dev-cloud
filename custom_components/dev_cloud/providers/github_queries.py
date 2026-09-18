"""GraphQL documents for the GitHub provider.

Kept apart from the provider so the query text — which is where the node-budget and
pagination constraints actually live — can be read and changed on its own.

GitHub rejects a query whose *product* of `first` values along any path exceeds 500,000
nodes, so the nested connections are smaller than the outer one:
100 repositories x 50 releases x 50 assets = 250,000. Anything past those nested page sizes
is collected by the per-repository and per-release follow-up queries.
"""

from __future__ import annotations

# GraphQL connection page sizes; 100 is the documented per-connection maximum.
GRAPHQL_PAGE_SIZE = 100
GRAPHQL_NESTED_PAGE_SIZE = 50

RELEASE_FIELDS = """
  id
  name
  tagName
  publishedAt
  url
  releaseAssets(first: $nested) {
    pageInfo { hasNextPage endCursor }
    nodes { name downloadCount }
  }
"""

USER_RELEASES_QUERY = f"""
query($login: String!, $cursor: String, $size: Int!, $nested: Int!) {{
  user(login: $login) {{
    repositories(
      first: $size,
      after: $cursor,
      ownerAffiliations: [OWNER],
      orderBy: {{field: PUSHED_AT, direction: DESC}}
    ) {{
      pageInfo {{ hasNextPage endCursor }}
      nodes {{
        nameWithOwner
        releases(first: $nested, orderBy: {{field: CREATED_AT, direction: DESC}}) {{
          pageInfo {{ hasNextPage endCursor }}
          nodes {{ {RELEASE_FIELDS} }}
        }}
      }}
    }}
  }}
}}
"""

REPO_RELEASES_QUERY = f"""
query($owner: String!, $name: String!, $cursor: String, $size: Int!, $nested: Int!) {{
  repository(owner: $owner, name: $name) {{
    releases(first: $size, after: $cursor, orderBy: {{field: CREATED_AT, direction: DESC}}) {{
      pageInfo {{ hasNextPage endCursor }}
      nodes {{ {RELEASE_FIELDS} }}
    }}
  }}
}}
"""

ORG_RELEASES_QUERY = f"""
query($login: String!, $cursor: String, $size: Int!, $nested: Int!) {{
  organization(login: $login) {{
    repositories(first: $size, after: $cursor, orderBy: {{field: PUSHED_AT, direction: DESC}}) {{
      pageInfo {{ hasNextPage endCursor }}
      nodes {{
        nameWithOwner
        releases(first: $nested, orderBy: {{field: CREATED_AT, direction: DESC}}) {{
          pageInfo {{ hasNextPage endCursor }}
          nodes {{ {RELEASE_FIELDS} }}
        }}
      }}
    }}
  }}
}}
"""

SPONSORS_QUERY = """
query($login: String!) {
  user(login: $login) {
    sponsorshipsAsMaintainer(activeOnly: true) { totalCount }
    sponsorshipsAsSponsor(activeOnly: true) { totalCount }
  }
}
"""

ASSETS_QUERY = """
query($id: ID!, $cursor: String, $size: Int!) {
  node(id: $id) {
    ... on Release {
      releaseAssets(first: $size, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes { name downloadCount }
      }
    }
  }
}
"""
