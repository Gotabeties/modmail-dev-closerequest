
# Modmail Dev Close Request

Allow your ticket staff to send a request to the customer if they want to close their ticket.

```?plugin add Gotabeties/modmail-dev-closerequest/closerequest@master```

# Claim

Lets supporters claim a ticket by appending the first five letters of their name to the ticket channel name.

```?plugin add Gotabeties/modmail-dev-closerequest/claim@master```
```?claim```
```?unclaim```

# Modmail Dev Response Time

Reports response time of tickets from ticket creation to first response.

```?plugin add Gotabeties/modmail-dev-closerequest/responsetime@master```

# Uptime Ping

Sends HEAD, GET, or POST requests to a configurable URL every minute (or custom interval)

```?plugin add Gotabeties/modmail-dev-closerequest/uptimeping@master```
```?httpping```

# Hiring Form

Adds a Hiring Request Menu panel with private controls to add, edit, and delete hiring requests.

```?plugin add Gotabeties/modmail-dev-closerequest/hiring@master```
```?hiringconfig```

# AI Ticket Auto Reply (Hermes API)

Auto replies with AI only in the ticket channels/categories you configure, using a Hermes-compatible OpenAI endpoint.
If a user asks for a real person, the ticket channel can be moved to a separate escalation category.
Works with Modmail relays where inbound user messages appear in staff channels via webhook or bot embed cards.

```?plugin add Gotabeties/modmail-dev-closerequest/aiticket@master```
```?aiticket```

Quick setup:

```?aiticket setbaseurl https://hermes.ai.unturf.com/v1```
```?aiticket setapikey choose-any-value```
```?aiticket setmodel adamo1139/Hermes-3-Llama-3.1-8B-FP8-Dynamic```
```?aiticket addcategory <your-ticket-category>```
```?aiticket setescalationcategory <your-human-escalation-category>```
```?aiticket toggle```

Useful config commands:

```?aiticket status```
```?aiticket addchannel #ticket-channel```
```?aiticket removechannel #ticket-channel```
```?aiticket addcategory <category>```
```?aiticket removecategory <category>```
```?aiticket settemperature <0.0-2.0>```
```?aiticket setmaxtokens <20-2000>```
```?aiticket sethistory <1-20>```
```?aiticket setcooldown <0-600>```
```?aiticket setprompt <system prompt>```
```?aiticket setthinking <status message>```
```?aiticket setescalateonerror <true/false>```
```?aiticket test [optional prompt]```
```?aiticket addkeyword <phrase>```
```?aiticket removekeyword <phrase>```
