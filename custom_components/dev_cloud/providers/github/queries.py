"""GraphQL documents for the GitHub provider.

Kept apart from the provider so the query text — which is where the node-budget and
pagination constraints actually live — can be read and changed on its own.

GitHub bills GraphQL in *points*, not requests: roughly one point per hundred nodes the
query asks for, counted from the `first` values multiplied down each path — whether or not
that many exist. A personal access token gets 5000 points an hour.

That makes the page sizes a budget decision, not just a validity one. 100 repositories x 50
releases x 50 assets is 255,100 nodes, about 2551 points: two such queries exhaust the hourly
budget, and walking 45 organisations would need 45 more. The sizes below cost roughly 28
points per query instead, at the price of more round trips. Anything past the nested sizes is
collected by the per-repository and per-release follow-up queries, which are only issued for
the repositories that actually have more.
"""

from __future__ import annotations

# Deliberately far below the per-connection maximum of 100; see the points budget above.
GRAPHQL_PAGE_SIZE = 25
GRAPHQL_NESTED_PAGE_SIZE = 10
#: Branches and tags are cheap per item, but cost is charged on what a query *asks* for, not
#: what exists — so a generous page would bill every repository for refs it does not have.
#: Repositories with more than this are completed by a follow-up query, which is rare.
GRAPHQL_REFS_PAGE_SIZE = 50

REF_FIELDS = """
  pageInfo { hasNextPage endCursor }
  nodes { name target { oid } }
"""

#: The real watcher count. REST's `watchers_count` is a deprecated alias for
#: `stargazers_count`, and `subscribers_count` — which is the true figure — is only returned
#: by the single-repository endpoint. Asking here costs one node per repository instead of
#: one request per repository.
REPO_WATCHERS = """
  watchers { totalCount }
"""

REPO_REFS = f"""
  branches: refs(refPrefix: "refs/heads/", first: {GRAPHQL_REFS_PAGE_SIZE}) {{ {REF_FIELDS} }}
  tags: refs(refPrefix: "refs/tags/", first: {GRAPHQL_REFS_PAGE_SIZE}) {{ {REF_FIELDS} }}
"""

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

RATE_LIMIT_FIELDS = """
  rateLimit { cost remaining resetAt }
"""

USER_RELEASES_QUERY = f"""
query($login: String!, $cursor: String, $size: Int!, $nested: Int!) {{
  {RATE_LIMIT_FIELDS}
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
        {REPO_WATCHERS}
        {REPO_REFS}
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
  {RATE_LIMIT_FIELDS}
  organization(login: $login) {{
    repositories(first: $size, after: $cursor, orderBy: {{field: PUSHED_AT, direction: DESC}}) {{
      pageInfo {{ hasNextPage endCursor }}
      nodes {{
        nameWithOwner
        {REPO_WATCHERS}
        {REPO_REFS}
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

REFS_QUERY = f"""
query($owner: String!, $name: String!, $prefix: String!, $cursor: String) {{
  repository(owner: $owner, name: $name) {{
    refs(refPrefix: $prefix, first: {GRAPHQL_REFS_PAGE_SIZE}, after: $cursor) {{
      {REF_FIELDS}
    }}
  }}
}}
"""
