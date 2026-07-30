"""GraphQL operations for Twitch API interactions."""

from __future__ import annotations

from .constants import GQLOperation


CAMPAIGN_IN_PROGRESS_FRAGMENT = """
fragment campaignInProgress on DropCampaign {
  id
  detailsURL
  accountLinkURL
  startAt
  endAt
  imageURL
  name
  status
  self {
    isAccountConnected
  }
  game {
    id
    slug
    name
    boxArtURL(width: 285, height: 380)
  }
  allow {
    channels {
      id
      name
      url
    }
  }
  eventBasedDrops {
    id
    name
    startAt
    endAt
    benefitEdges {
      benefit {
        id
        imageAssetURL
        name
        distributionType
      }
      entitlementLimit
    }
    campaign {
      id
      detailsURL
      self {
        isAccountConnected
      }
    }
    localizedContent {
      ... on DropTypeInventoryLocalizedContent {
        progress
      }
    }
  }
  timeBasedDrops {
    id
    name
    startAt
    endAt
    preconditionDrops {
      id
    }
    requiredMinutesWatched
    requiredSubs
    benefitEdges {
      benefit {
        id
        imageAssetURL
        name
        distributionType
      }
      entitlementLimit
      claimCount
    }
    self {
      hasPreconditionsMet
      currentMinutesWatched
      currentSubs
      isClaimed
      dropInstanceID
    }
    campaign {
      id
      detailsURL
      accountLinkURL
      self {
        isAccountConnected
      }
    }
    localizedContent {
      ... on DropTypeInventoryLocalizedContent {
        progress
      }
    }
  }
}
""".strip()

DROP_CAMPAIGN_FRAGMENT = """
fragment dropCampaign on DropCampaign {
  id
  name
  owner {
    id
    name
  }
  game {
    id
    displayName
    boxArtURL(width: 120, height: 160)
  }
  status
  startAt
  endAt
  detailsURL
  accountLinkURL
  self {
    isAccountConnected
  }
}
""".strip()

INVENTORY_QUERY = f"""
query Inventory {{
  currentUser {{
    inventory {{
      dropCampaignsInProgress {{
        ...campaignInProgress
      }}
      gameEventDrops {{
        id
        lastAwardedAt
      }}
    }}
  }}
}}

{CAMPAIGN_IN_PROGRESS_FRAGMENT}
""".strip()

CAMPAIGNS_QUERY = f"""
query ViewerDropsDashboard {{
  currentUser {{
    id
    login
    dropCampaigns {{
      ...dropCampaign
    }}
  }}
}}

{DROP_CAMPAIGN_FRAGMENT}
""".strip()

CAMPAIGN_DETAILS_QUERY = f"""
query DropCampaignDetails($channelLogin: ID!, $dropID: ID!) {{
  user(id: $channelLogin) {{
    dropCampaign(id: $dropID) {{
      ...campaignInProgress
    }}
  }}
}}

{CAMPAIGN_IN_PROGRESS_FRAGMENT}
""".strip()

CLAIM_DROP_QUERY = """
mutation DropsPage_ClaimDropRewards($input: ClaimDropRewardsInput!) {
  claimDropRewards(input: $input) {
    status
    isUserAccountConnected
    dropType {
      id
      campaign {
        id
        detailsURL
      }
    }
  }
}
""".strip()


GQL_OPERATIONS: dict[str, GQLOperation] = {
    # returns stream information for a particular channel
    "GetStreamInfo": GQLOperation(
        "VideoPlayerStreamInfoOverlayChannel",
        "a5f2e34d626a9f4f5c0204f910bab2194948a9502089be558bb6e779a9e1b3d2",
        variables={
            "channel": ...,  # channel login
        },
    ),
    # can be used to claim channel points
    "ClaimCommunityPoints": GQLOperation(
        "ClaimCommunityPoints",
        "46aaeebe02c99afdf4fc97c7c0cba964124bf6b0af229395f1f6d1feed05b3d0",
        variables={
            "input": {
                "claimID": ...,  # points claim_id
                "channelID": ...,  # channel ID as a str
            },
        },
    ),
    # can be used to claim a drop
    "ClaimDrop": GQLOperation(
        "DropsPage_ClaimDropRewards",
        query=CLAIM_DROP_QUERY,
        variables={
            "input": {
                "dropInstanceID": ...,  # drop claim_id
            },
        },
    ),
    # returns current state of points (balance, claim available) for a particular channel
    "ChannelPointsContext": GQLOperation(
        "ChannelPointsContext",
        "9988086babc615a918a1e9a722ff41d98847acac822645209ac7379eecb27152",
        variables={
            "channelLogin": ...,  # channel login
        },
    ),
    # returns all in-progress campaigns
    "Inventory": GQLOperation(
        "Inventory",
        query=INVENTORY_QUERY,
    ),
    # returns current state of drops (current drop progress)
    "CurrentDrop": GQLOperation(
        "DropCurrentSessionContext",
        "4d06b702d25d652afb9ef835d2a550031f1cf762b193523a92166f40ea3d142b",
        variables={
            "channelID": ...,  # watched channel ID as a str
            "channelLogin": "",  # always empty string
        },
    ),
    # returns all available campaigns
    "Campaigns": GQLOperation(
        "ViewerDropsDashboard",
        query=CAMPAIGNS_QUERY,
    ),
    # returns extended information about a particular campaign
    "CampaignDetails": GQLOperation(
        "DropCampaignDetails",
        query=CAMPAIGN_DETAILS_QUERY,
        variables={
            "channelLogin": ...,  # user ID as a str
            "dropID": ...,  # campaign ID
        },
    ),
    # returns drops available for a particular channel
    "AvailableDrops": GQLOperation(
        "DropsHighlightService_AvailableDrops",
        "b19ee96a0e79e3f8281c4108bc4c7b3f232266db6f96fd04a339ab393673a075",
        variables={
            "channelID": ...,  # channel ID as a str
        },
    ),
    # retuns stream playback access token
    "PlaybackAccessToken": GQLOperation(
        "PlaybackAccessToken",
        "ed230aa1e33e07eebb8928504583da78a5173989fadfb1ac94be06a04f3cdbe9",
        variables={
            "isLive": True,
            "isVod": False,
            "login": ...,  # channel login
            "platform": "web",
            "playerType": "site",
            "vodID": "",
        },
    ),
    # returns live channels for a particular game
    "GameDirectory": GQLOperation(
        "DirectoryPage_Game",
        "cb5dc816e139dcb8a118f14b4b677d59abc224a4b016c4bc2bb00a47fe0ddec4",
        variables={
            "limit": 30,  # limit of channels returned
            "slug": ...,  # game slug
            "imageWidth": 50,
            "includeCostreaming": False,
            "options": {
                "broadcasterLanguages": [],
                "freeformTags": None,
                "includeRestricted": ["SUB_ONLY_LIVE"],
                "recommendationsContext": {"platform": "web"},
                "sort": "RELEVANCE",  # also accepted: "VIEWER_COUNT"
                "systemFilters": [],
                "tags": [],
                "requestID": "JIRA-VXP-2397",
            },
            "sortTypeIsRecency": False,
        },
    ),
    "SlugRedirect": GQLOperation(  # can be used to turn game name -> game slug
        "DirectoryGameRedirect",
        "1f0300090caceec51f33c5e20647aceff9017f740f223c3c532ba6fa59f6b6cc",
        variables={
            "name": ...,  # game name
        },
    ),
    "NotificationsView": GQLOperation(  # unused, triggers notifications "update-summary"
        "OnsiteNotifications_View",
        "e8e06193f8df73d04a1260df318585d1bd7a7bb447afa058e52095513f2bfa4f",
        variables={
            "input": {},
        },
    ),
    "NotificationsList": GQLOperation(  # unused
        "OnsiteNotifications_ListNotifications",
        "11cdb54a2706c2c0b2969769907675680f02a6e77d8afe79a749180ad16bfea6",
        variables={
            "cursor": "",
            "displayType": "VIEWER",
            "language": "en",
            "limit": 10,
            "shouldLoadLastBroadcast": False,
        },
    ),
    "NotificationsDelete": GQLOperation(
        "OnsiteNotifications_DeleteNotification",
        "13d463c831f28ffe17dccf55b3148ed8b3edbbd0ebadd56352f1ff0160616816",
        variables={
            "input": {
                "id": "",  # ID of the notification to delete
            }
        },
    ),
}
