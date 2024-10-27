""" api: bancho.py's developer api for interacting with server state """
from __future__ import annotations

import datetime
import hashlib
import struct
from pathlib import Path as SystemPath
from typing import Literal

from fastapi import APIRouter
from fastapi import Depends
from fastapi import status
from fastapi.param_functions import Query
from fastapi.responses import ORJSONResponse
from fastapi.responses import Response
from fastapi.security import HTTPAuthorizationCredentials as HTTPCredentials
from fastapi.security import HTTPBearer

import app.packets
import app.settings
import app.state
import app.usecases.performance
from app.constants import regexes
from app.constants.gamemodes import GameMode
from app.constants.mods import Mods
from app.objects.beatmap import Beatmap
from app.objects.beatmap import ensure_local_osu_file
from app.objects.clan import Clan
from app.objects.player import Player
from app.repositories import players as players_repo
from app.repositories import scores as scores_repo
from app.repositories import stats as stats_repo
from app.usecases.performance import ScoreParams
from app.constants.privileges import Privileges
# Import discord webhook
from app.discord import Webhook, Embed
from app.repositories import maps as maps_repo
AVATARS_PATH = SystemPath.cwd() / ".data/avatars"
BEATMAPS_PATH = SystemPath.cwd() / ".data/osu"
REPLAYS_PATH = SystemPath.cwd() / ".data/osr"
SCREENSHOTS_PATH = SystemPath.cwd() / ".data/ss"


router = APIRouter()
oauth2_scheme = HTTPBearer(auto_error=False)

# NOTE: the api is still under design and is subject to change.
# to keep up with breaking changes, please either join our discord,
# or keep up with changes to https://github.com/JKBGL/gulag-api-docs.

# Unauthorized (no api key required)
# GET /search_players: returns a list of matching users, based on a passed string, sorted by ascending ID.
# GET /get_player_count: return total registered & online player counts.
# GET /get_player_info: return info or stats for a given player.
# GET /get_player_status: return a player's current status, if online.
# GET /get_player_scores: return a list of best or recent scores for a given player.
# GET /get_player_most_played: return a list of maps most played by a given player.
# GET /get_map_info: return information about a given beatmap.
# GET /get_map_scores: return the best scores for a given beatmap & mode.
# GET /get_score_info: return information about a given score.
# GET /get_replay: return the file for a given replay (with or without headers).
# GET /get_match: return information for a given multiplayer match.
# GET /get_leaderboard: return the top players for a given mode & sort condition

# Authorized (requires valid api key, passed as 'Authorization' header)
# NOTE: authenticated handlers may have privilege requirements.

# [Normal]
# GET /calculate_pp: calculate & return pp for a given beatmap.
# POST/PUT /set_avatar: Update the tokenholder's avatar to a given file.

# TODO handlers
# GET /get_friends: return a list of the player's friends.
# POST/PUT /set_player_info: update user information (updates whatever received).

DATETIME_OFFSET = 0x89F7FF5F7B58000


def format_clan_basic(clan: Clan) -> dict[str, object]:
    return {
        "id": clan.id,
        "name": clan.name,
        "tag": clan.tag,
        "members": len(clan.member_ids),
    }


def format_player_basic(player: Player) -> dict[str, object]:
    return {
        "id": player.id,
        "name": player.name,
        "country": player.geoloc["country"]["acronym"],
        "clan": format_clan_basic(player.clan) if player.clan else None,
        "online": player.is_online,
    }


def format_map_basic(m: Beatmap) -> dict[str, object]:
    return {
        "id": m.id,
        "md5": m.md5,
        "set_id": m.set_id,
        "artist": m.artist,
        "title": m.title,
        "version": m.version,
        "creator": m.creator,
        "last_update": m.last_update,
        "total_length": m.total_length,
        "max_combo": m.max_combo,
        "status": m.status,
        "plays": m.plays,
        "passes": m.passes,
        "mode": m.mode,
        "bpm": m.bpm,
        "cs": m.cs,
        "od": m.od,
        "ar": m.ar,
        "hp": m.hp,
        "diff": m.diff,
    }


@router.get("/calculate_pp")
async def api_calculate_pp(
    token: HTTPCredentials = Depends(oauth2_scheme),
    beatmap_id: int = Query(None, alias="id", min=0, max=2_147_483_647),
    nkatu: int = Query(None, max=2_147_483_647),
    ngeki: int = Query(None, max=2_147_483_647),
    n100: int = Query(None, max=2_147_483_647),
    n50: int = Query(None, max=2_147_483_647),
    misses: int = Query(0, max=2_147_483_647),
    mods: int = Query(0, min=0, max=2_147_483_647),
    mode: int = Query(0, min=0, max=11),
    combo: int = Query(None, max=2_147_483_647),
    acclist: list[float] = Query([100, 99, 98, 95], alias="acc"),
) -> Response:
    """Calculates the PP of a specified map with specified score parameters."""

    if token is None or app.state.sessions.api_keys.get(token.credentials) is None:
        return ORJSONResponse(
            {"status": "Invalid API key."},
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    beatmap = await Beatmap.from_bid(beatmap_id)
    if not beatmap:
        return ORJSONResponse(
            {"status": "Beatmap not found."},
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    if not await ensure_local_osu_file(
        BEATMAPS_PATH / f"{beatmap.id}.osu",
        beatmap.id,
        beatmap.md5,
    ):
        return ORJSONResponse(
            {"status": "Beatmap file could not be fetched."},
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    scores = []

    if all(x is None for x in [ngeki, nkatu, n100, n50]):
        scores = [
            ScoreParams(GameMode(mode).as_vanilla, mods, combo, acc, nmiss=misses)
            for acc in acclist
        ]
    else:
        scores.append(
            ScoreParams(
                GameMode(mode).as_vanilla,
                mods,
                combo,
                ngeki=ngeki or 0,
                nkatu=nkatu or 0,
                n100=n100 or 0,
                n50=n50 or 0,
                nmiss=misses,
            ),
        )

    results = app.usecases.performance.calculate_performances(
        str(BEATMAPS_PATH / f"{beatmap.id}.osu"),
        scores,
    )

    # "Inject" the accuracy into the list of results
    final_results = [
        performance_result | {"accuracy": score.acc}
        for performance_result, score in zip(results, scores)
    ]

    return ORJSONResponse(
        # XXX: change the output type based on the inputs from user
        final_results
        if all(x is None for x in [ngeki, nkatu, n100, n50])
        else final_results[0],
        status_code=status.HTTP_200_OK,  # a list via the acclist parameter or a single score via n100 and n50
    )


@router.get("/search_players")
async def api_search_players(
    search: str | None = Query(None, alias="q", min=2, max=32),
    limit: int | None = Query(10, alias="l", min=1, max=100)
) -> Response:
    """Search for users on the server by name."""
    rows = await app.state.services.database.fetch_all(
        "SELECT id, name "
        "FROM users "
        "WHERE name LIKE COALESCE(:name, name) "
        "AND priv & 3 = 3 "
        "ORDER BY id ASC "
        "LIMIT :limit",
        {"name": f"%{search}%" if search is not None else None, "limit": limit},
    )

    return ORJSONResponse(
        {
            "status": "success",
            "results": len(rows),
            "result": [dict(row) for row in rows],
        },
    )


@router.get("/get_player_count")
async def api_get_player_count() -> Response:
    """Get the current amount of online players."""
    return ORJSONResponse(
        {
            "status": "success",
            "counts": {
                # -1 for the bot, who is always online
                "online": len(app.state.sessions.players) - 1,
                "total": await players_repo.fetch_count(),
            },
        },
    )


@router.get("/get_player_info")
async def api_get_player_info(
    scope: Literal["ranks", "stats", "info", "all"],
    user_id: int | None = Query(None, alias="id", ge=3, le=2_147_483_647),
    username: str | None = Query(None, alias="name", pattern=regexes.USERNAME.pattern),
) -> Response:
    """Return information about a given player."""
    if not (username or user_id) or (username and user_id):
        return ORJSONResponse(
            {"status": "Must provide either id OR name!"},
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    # get user info from username or user id
    if username:
        user_info = await players_repo.fetch_one(name=username)
    else:  # if user_id
        user_info = await players_repo.fetch_one(id=user_id)

    if user_info is None:
        return ORJSONResponse(
            {"status": "Player not found."},
            status_code=status.HTTP_404_NOT_FOUND,
        )

    resolved_user_id: int = user_info["id"]
    resolved_country: str = user_info["country"]

    api_data = {}
    
    # fetch user's info if requested
    if scope in ("info", "all"):
        api_data["info"] = dict(user_info)
        api_data["info"].pop("discord_id", None)

    # fetch user's stats if requested
    if scope in ("stats", "all"):
        api_data["stats"] = {}

        # get all stats
        all_stats = await stats_repo.fetch_many(player_id=resolved_user_id)

        for mode_stats in all_stats:
            rank = await app.state.services.redis.zrevrank(
                f"bancho:leaderboard:{mode_stats['mode']}",
                str(resolved_user_id),
            )
            country_rank = await app.state.services.redis.zrevrank(
                f"bancho:leaderboard:{mode_stats['mode']}:{resolved_country}",
                str(resolved_user_id),
            )

            # NOTE: this dict-like return is intentional.
            #       but quite cursed.
            stats_key = str(mode_stats["mode"])
            api_data["stats"][stats_key] = {
                "id": mode_stats["id"],
                "mode": mode_stats["mode"],
                "tscore": mode_stats["tscore"],
                "rscore": mode_stats["rscore"],
                "pp": mode_stats["pp"],
                "plays": mode_stats["plays"],
                "playtime": mode_stats["playtime"],
                "acc": mode_stats["acc"],
                "max_combo": mode_stats["max_combo"],
                "total_hits": mode_stats["total_hits"],
                "replay_views": mode_stats["replay_views"],
                "xh_count": mode_stats["xh_count"],
                "x_count": mode_stats["x_count"],
                "sh_count": mode_stats["sh_count"],
                "s_count": mode_stats["s_count"],
                "a_count": mode_stats["a_count"],
                # extra fields are added to the api response
                "rank": rank + 1 if rank is not None else 0,
                "country_rank": country_rank + 1 if country_rank is not None else 0,
            }


    return ORJSONResponse({"status": "success", "player": api_data})

# /get_player_whitelist
# We need id, return did they have whitelist or not
@router.get("/get_player_whitelist")
async def api_get_player_whitelist(
    user_id: int | None = Query(None, alias="id", ge=3, le=2_147_483_647),
    username: str | None = Query(None, alias="name", pattern=regexes.USERNAME.pattern),
) -> Response:
    if not (username or user_id) or (username and user_id):
        return ORJSONResponse(
            {"status": "Must provide either id OR name!"},
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    if username:
        user_info = await players_repo.fetch_one(name=username)
        # if user is whitelisted, return true
        if user_info["priv"] & Privileges.Whitelisted:
            whitelist = True
        else:
            whitelist = False
    else:
        user_info = await players_repo.fetch_one(id=user_id)
        if user_info["priv"] & Privileges.WHITELISTED:
            whitelist = True
        else:
            whitelist = False
    return ORJSONResponse(
        {
            "status": "success",
            "whitelist": whitelist,
        },
    )

# router.get("/vote_beatmap")
# This API is not for everyone, this api required match osu!api key with config
# This API Required, discord id, and beatmap set id
# If discord is is not exist in users table, return error
# If discord id is match but user is not NAT or Nominate, return error
# If discord id is match and user is NAT or Nominate, return success
# Remember, we need api key to match with osu!api key in config
# They should to be like /vote_beatmap?discord_id=736163902835916880&set_id=7723321&key=osu!api key
# If osu!api key is not match with config, return error
# Also discord userid is 18 digit, if not return error
# DO NOT LIMIT DIGTS OF DISCORD ID OR SET ID
@router.get("/vote_beatmap")
async def api_vote_beatmap(
    discord_id: int | None = Query(None, alias="discord_id", ge=100000000000000000, le=999999999999999999),
    set_id: int | None = Query(None, alias="set_id", ge=0, le=2_147_483_647),
    key: str | None = Query(None, alias="key", min_length=1, max_length=64),
) -> Response:
    # Print discord_id, set_id, and key
    print(discord_id, set_id, key)
    if not discord_id or not set_id or not key:
        # 400 bad request, response = "Missing required parameters!"
        return ORJSONResponse(
            {
                "success": False,
                "response": {
                    "message": "Missing required parameters!"
                },
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    # check did discord id is exist in users table (please check by database)
    # Please use app.state.services.database.fetch_val
    user_info = await app.state.services.database.fetch_val(
        "SELECT * FROM users WHERE discord_id = :discord_id",
        {"discord_id": discord_id},
    )
    print(user_info)
    if not user_info:
        # 404 not found, response = "User not found!"
        return ORJSONResponse(
            {
                "success": False,
                "response": {
                    "message": "User not found!"
                },
            },
            status_code=status.HTTP_404_NOT_FOUND,
        )
    user_info = await players_repo.fetch_one(id=user_info)

    if not user_info["priv"] & (Privileges.NOMINATOR | Privileges.NAT):
        # 403 forbidden, response = "You are not a nominator or NAT!"
        return ORJSONResponse(
            {
                "success": False,
                "response": {
                    "message": "You are not a nominator or NAT!"
                },
            },
            status_code=status.HTTP_403_FORBIDDEN,
        )

    if key != app.settings.OSU_API_KEY:
        # Sucess = false, 403 forbidden, response = "Invalid osu!api key"
        return ORJSONResponse(
            {
                "success": False,
                "response": {
                    "message": "Invalid osu!api key!"
                },
            },
            status_code=status.HTTP_403_FORBIDDEN,
        )
    # Check did beatmap set id is already ranked or not
    # Please use app.state.services.database.fetch_val
    # If status = 2, that mean beatmap is already ranked
    # If status = 3, that mean beatmap is already loved
    # If status = 4, that mean beatmap is already qualified
    # We will vote only if status is 0 or 1
    beatmap_info = await app.state.services.database.fetch_val(
        "SELECT status FROM maps WHERE set_id = :set_id",
        {"set_id": set_id},
    )
    if beatmap_info in (2, 3, 4, 5):
        # status is true but response is already ranked, 403 forbidden
        # do like success but do not include vote count and need count
        return ORJSONResponse(
            {
                "success": False,
                "response": {
                    "message": "Beatmap is already ranked, loved, or qualified!"
                },
            },
            status_code=status.HTTP_403_FORBIDDEN,
        )
    else:
        # Check did user already vote or not in redis
        # If user already vote, return error
        # If user not vote, return success
        # Please use app.state.services.redis.get
        userid = user_info["id"]
        vote = await app.state.services.redis.get(f"vote:{userid}:{set_id}")
        # If user already vote
        if vote:
            # status is true but response is already voted, 403 forbidden
            # do like success but do not include vote count and need count
            return ORJSONResponse(
                {
                        "success": False,
                        "response": {
                            "message": "You have already voted this beatmap!"
                        },
                    },
                status_code=status.HTTP_403_FORBIDDEN,
            )
        else:
            # Check, did beatmap ever get vote or not
            # If beatmap already get vote, increment vote by 1
            # If beatmap not get vote, set vote to 1 and return like Now beatmap has 1/2 votes for qualification. One more vote is needed!
            
            # Check did beatmap already get vote or not
            # Please use app.state.services.redis.get
            # Check by if end with :set_id, that mean beatmap already get vote
            # If not, that mean beatmap not get vote
            # check did beatmap set_id is exist in database?
            # Please use app.state.services.database.fetch_val
            # If not exist, return error
            # If exist, continue
            beatmap_info = await app.state.services.database.fetch_val(
                "SELECT set_id FROM maps WHERE set_id = :set_id",
                {"set_id": set_id},
            )
            if not beatmap_info:
                # sucess = false, 404 not found, response = "Beatmap not found"
                return ORJSONResponse(
                    {
                        "success": False,
                        "response": {
                            "message": "Beatmap not found! (Please recheck your beatmap set_id!)"
                        },
                    },
                    status_code=status.HTTP_404_NOT_FOUND,
                )
            vote_key = f"vote:{userid}:{set_id}"
            # Define the key pattern
            vote_key_map = f"vote:*:{set_id}"
            vote_key_pattern = f"vote:{userid}:{set_id}"

            # Use scan_iter to get an async iterator of keys that match the pattern
            vote_keys = app.state.services.redis.scan_iter(match=vote_key_pattern)
            vote_maps = app.state.services.redis.scan_iter(match=vote_key_map)
            # Create an empty list to store the keys
            vote_keys_list = []
            vote_maps_list = []
            # Use an async for loop to iterate over the async generator
            async for key in vote_keys:
                vote_keys_list.append(key)
            async for key in vote_maps:
                vote_maps_list.append(key)

            # Initialize vote_count
            vote_count = None

            # Get the vote count from Redis
            vote_count = len(vote_maps_list)
            print(vote_count)
            if vote_count is None:
                await app.state.services.redis.set(vote_key, 1)
                vote_count = '1'
            elif isinstance(vote_count, int):
                await app.state.services.redis.incr(vote_key)
                vote_count = str(vote_count + 1)
            else:
                return "Error: vote count is not an integer."
            print(vote_count)
            if vote_count == '1':
                # Send webhook to discord
                # Getting beatmap info by database
                beatmap_info = await app.state.services.database.fetch_one(
                    "SELECT * FROM maps WHERE set_id = :set_id",
                    {"set_id": set_id},
                )
                print(beatmap_info)
                # Get username of user
                username = user_info["name"]
                # We need artist, title, version, creator, and set_id
                artist = beatmap_info["artist"]
                title = beatmap_info["title"]
                creator = beatmap_info["creator"]
                # Send to discord webhook nomination
                webhook = Webhook(url=app.settings.DISCORD_QUALIFIED_WEBHOOK)
                # Tell beatmap info and clickable link to osu!bancho
                # like [artist - title (version) by creator](https://osu.ppy.sh/beatmapsets/set_id) has 1/2 votes for qualification. One more vote is needed!
                embed = Embed(
                    title="Beatmap Nomination",
                    description=f"[{artist} - {title} by {creator}](https://osu.ppy.sh/beatmapsets/{set_id}) has 1/2 votes for qualification. (Vote by {username}) One more vote is needed!",
                    color=0x808080
                )
                embed.set_image(url=f"https://assets.ppy.sh/beatmaps/{set_id}/covers/card.jpg")
                # Send the webhook
                webhook.add_embed(embed)
                await webhook.post()
                # Make respone like this
                #{
                #    "status": true,
                #    "response": {
                #        "message": "You have nominated this map, this mapset need 1 more vote for qualified status",
                #        "votes": 1,
                #        "need" : 1
                #    }
                #}
                # status will be true because they are successfully
                return ORJSONResponse(
                    {
                        "success": True,
                        "response": {
                            "message": "You have nominated this map, this mapset need 1 more vote for qualified status!",
                            "votes": int(vote_count),
                            "need": 1
                        },
                    },
                    status_code=status.HTTP_200_OK,
                )
            else:
                print(vote_count)
                print("Qualified")
                # Qualified beatmap, edit value in database to 4
                await app.state.services.database.execute(
                    "UPDATE maps SET status = 4 WHERE set_id = :set_id",
                    {"set_id": set_id},
                )
                # delete vote from redis key
                await app.state.services.redis.delete(vote_key)
                # set beatmap change_date to current time
                await app.state.services.database.execute(
                    "UPDATE maps SET change_date = now() WHERE set_id = :set_id",
                    {"set_id": set_id}
                )
                    
                # Get beatmap id, not set id
                ids = await app.state.services.database.fetch_all(
                "SELECT id FROM maps WHERE set_id = :set_id",
                {"set_id": set_id}
                )
                for idmap in ids:
                    print(idmap)
                    md5 = await app.state.services.database.fetch_val(
                        "SELECT md5 FROM maps WHERE id = :id",
                        {"id": idmap}
                    )
                    if md5 in app.state.cache.beatmap:
                        app.state.cache.beatmap[md5].status = 2
                        app.state.cache.beatmap[md5].frozen = True
                    # delete request from map_requests (map_id)
                    await app.state.services.database.execute(
                        "DELETE FROM map_requests WHERE map_id = :id",
                        {"id": idmap}
                    )
                    
                # Send to discord webhook
                # Getting beatmap info by database
                beatmap_info = await app.state.services.database.fetch_one(
                    "SELECT * FROM maps WHERE set_id = :set_id",
                    {"set_id": set_id},
                )
                # Send to discord webhook qualification
                
                # We need artist, title, version, creator, and set_id
                artist = beatmap_info["artist"]
                title = beatmap_info["title"]
                creator = beatmap_info["creator"]
                # Get username of user
                username = user_info["name"]
                if webhook_url := app.settings.DISCORD_QUALIFIED_WEBHOOK:
                    embed = Embed(title="", description=f"[{artist} - {title} ({creator})](https://osu.ppy.sh/beatmapsets/{set_id}) is now qualified! (Lastest vote by {username})", timestamp=datetime.datetime.utcnow(), color=52478)
                    embed.set_author(name="Automatic Status Bot (Click to get beatmap!)", icon_url="https://a.ppy.sh/1", url=f"https://osu.ppy.sh/beatmapsets/{set_id}")
                    embed.set_image(url=f"https://assets.ppy.sh/beatmaps/{set_id}/covers/card.jpg")
                    embed.set_footer(text="Nomination Tools")
                    embed.color = 0x00FF00
                    webhook = Webhook(webhook_url, embeds=[embed])
                await webhook.post()
                if webhook_url := app.settings.DISCORD_NOMINATION_WEBHOOK:
                    embed = Embed(title="", description=f"[{artist} - {title} ({creator})](https://osu.ppy.sh/beatmapsets/{set_id}) is now qualified!", timestamp=datetime.datetime.utcnow(), color=52478)
                    embed.set_author(name="Automatic Status Bot (Click to get beatmap!)", icon_url="https://a.ppy.sh/1", url=f"https://osu.ppy.sh/beatmapsets/{set_id}")
                    embed.set_image(url=f"https://assets.ppy.sh/beatmaps/{set_id}/covers/card.jpg")
                    embed.set_footer(text="Nomination Tools")
                    embed.color = 0x00FF00
                    webhook = Webhook(webhook_url, embeds=[embed])
                await webhook.post()
                    
                return ORJSONResponse(
                    {
                        "success": True,
                        "response": {
                            "message": "You have nominated this map, this mapset has been qualified!",
                            "votes": int(vote_count),
                            "need": 0
                        },
                    },
                    status_code=status.HTTP_200_OK,
                )
# Give me example of api
# /vote_beatmap?discord_id=736163902835916880&set_id=772

# /love_beatmap, only for NAT, need discord id and set id and osu!api key that match with config
# they kinda same with vote beatmap, but this is for loved beatmap, and no need to vote, if beatmap is already love, return error, if beatmap is already ranked, return error
# If discord id is is not exist in users table, return error
# If discord id is match but user is not NAT, return error
# If discord id is match and user is NAT, return success
# Remember, we need api key to match with osu!api key in config
# They should to be like /love_beatmap?discord_id=736163902835916880&set_id=
# If osu!api key is not match with config, return error
# Also discord userid is 18 digit, if not return error
@router.get("/love_beatmap")
async def api_love_beatmap(
    discord_id: int | None = Query(None, alias="discord_id", ge=100000000000000000, le=999999999999999999),
    set_id: int | None = Query(None, alias="set_id", ge=0, le=2_147_483_647),
    key: str | None = Query(None, alias="key", min_length=1, max_length=64),
) -> Response:
    # Print discord_id, set_id, and key
    print(discord_id, set_id, key)
    if not discord_id or not set_id or not key:
        # 400 bad request, response = "Missing required parameters!"
        return ORJSONResponse(
            {
                "success": False,
                "response": {
                    "message": "Missing required parameters!"
                },
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    # check did discord id is exist in users table (please check by database)
    # Please use app.state.services.database.fetch_val
    user_info = await app.state.services.database.fetch_val(
        "SELECT * FROM users WHERE discord_id = :discord_id",
        {"discord_id": discord_id},
    )
    print(user_info)
    if not user_info:
        # 404 not found, response = "User not found!"
        return ORJSONResponse(
            {
                "success": False,
                "response": {
                    "message": "User not found!"
                },
            },
            status_code=status.HTTP_404_NOT_FOUND,
        )
    user_info = await players_repo.fetch_one(id=user_info)

    if not user_info["priv"] & Privileges.NAT:
        # 403 forbidden, response = "You are not a NAT!"
        return ORJSONResponse(
            {
                "success": False,
                "response": {
                    "message": "You are not a NAT!"
                },
            },
            status_code=status.HTTP_403_FORBIDDEN,
        )

    if key != app.settings.OSU_API_KEY:
        # Sucess = false, 403 forbidden, response = "Invalid osu!api key"
        return ORJSONResponse(
            {
                "success": False,
                "response": {
                    "message": "Invalid osu!api key!"
                },
            },
            status_code=status.HTTP_403_FORBIDDEN,
        )
    # Check did beatmap set id is already ranked or not
    # Please use app.state.services.database.fetch_val
    # If status = 2, that mean beatmap is already ranked
    # If status = 3, that mean beatmap is already loved
    # If status = 4, that mean beatmap is already qualified
    # We will vote only if status is 0 or 1
    beatmap_info = await app.state.services.database.fetch_val(
        "SELECT status FROM maps WHERE set_id = :set_id",
        {"set_id": set_id},
    )
    if beatmap_info in (2, 3, 4, 5):
        # status is true but response is already ranked, 403 forbidden
        # do like success but do not include vote count and need count
        return ORJSONResponse(
            {
                "success": False,
                "response": {
                    "message": "Beatmap is already ranked, loved, or qualified!"
                },
            },
            status_code=status.HTTP_403_FORBIDDEN,
        )
    else:
        # Check did beatmap set id is exist in database?
        # Please use app.state.services.database.fetch_val
        # If not exist, return error
        # If exist, continue
        beatmap_info = await app.state.services.database.fetch_val(
            "SELECT set_id FROM maps WHERE set_id = :set_id",
            {"set_id": set_id},
        )
        if not beatmap_info:
            # sucess = false, 404 not found, response = "Beatmap not found"
            return ORJSONResponse(
                {
                    "success": False,
                    "response": {
                        "message": "Beatmap not found! (Please recheck your beatmap set_id!)"
                    },
                },
                status_code=status.HTTP_404_NOT_FOUND,
            )
        # Loveable beatmap, edit value in database to 5
        print(set_id)
        await app.state.services.database.execute(
            "UPDATE maps SET status = 5 WHERE set_id = :set_id",
            {"set_id": set_id},
        )
        print("Loved")
        # set beatmap change_date to current time
        await app.state.services.database.execute(
            "UPDATE maps SET change_date = now() WHERE set_id = :set_id",
            {"set_id": set_id}
        )
        # Get beatmap id, not set id
        ids = await app.state.services.database.fetch_all(
        "SELECT id FROM maps WHERE set_id = :set_id",
        {"set_id": set_id}
        )
        for idmap in ids:
            idmap = idmap["id"]
            print(idmap)
            md5 = await app.state.services.database.fetch_val(
                "SELECT md5 FROM maps WHERE id = :id",
                {"id": idmap}
            )
            # update map by map_id
            await maps_repo.update(idmap, status=5, frozen=True)
            if md5 in app.state.cache.beatmap:
                app.state.cache.beatmap[md5].status = 5
                app.state.cache.beatmap[md5].frozen = True
            # delete request from map_requests (map_id)
            await app.state.services.database.execute(
                "DELETE FROM map_requests WHERE map_id = :id",
                {"id": idmap}
            )
        # Send to discord webhook
        # Getting beatmap info by database
        beatmap_info = await app.state.services.database.fetch_one(
            "SELECT * FROM maps WHERE set_id = :set_id",
            {"set_id": set_id},
        )
        # Send to discord webhook nomination
        # We need artist, title, version, creator, and set_id
        artist = beatmap_info["artist"]
        title = beatmap_info["title"]
        creator = beatmap_info["creator"]
        # Get username of user
        username = user_info["name"]
        if webhook_url := app.settings.DISCORD_NOMINATION_WEBHOOK:
            embed = Embed(title="", description=f"[{artist} - {title} ({creator})](https://osu.ppy.sh/beatmapsets/{set_id}) is now loved! (by {username})", timestamp=datetime.datetime.utcnow(), color=52478)
            embed.set_author(name="Automatic Status Bot (Click to get beatmap!)", icon_url="https://a.ppy.sh/1", url=f"https://osu.ppy.sh/beatmapsets/{set_id}")
            embed.set_image(url=f"https://assets.ppy.sh/beatmaps/{set_id}/covers/card.jpg")
            embed.set_footer(text="Nomination Tools")
            embed.color = 0xFF69B4
            webhook = Webhook(webhook_url, embeds=[embed])
        await webhook.post()
        return ORJSONResponse(
            {
                "success": True,
                "response": {
                    "message": "Now, this mapset has been loved!",
                },
            },
            status_code=status.HTTP_200_OK,
        )
# Give me example of api
# /love_beatmap?discord_id=736163902835916880&set_id=772?key=osu!api key

# /rank_beatmap, only for NAT, need discord id and set id and osu!api key that match with config
# they kinda same with love_beatmap, but this is for ranked beatmap, and no need to vote, if beatmap is already love, return error, if beatmap is already ranked, return error
@router.get("/rank_beatmap")
async def api_rank_beatmap(
    discord_id: int | None = Query(None, alias="discord_id", ge=100000000000000000, le=999999999999999999),
    set_id: int | None = Query(None, alias="set_id", ge=0, le=2_147_483_647),
    key: str | None = Query(None, alias="key", min_length=1, max_length=64),
) -> Response:
    # Print discord_id, set_id, and key
    print(discord_id, set_id, key)
    if not discord_id or not set_id or not key:
        # 400 bad request, response = "Missing required parameters!"
        return ORJSONResponse(
            {
                "success": False,
                "response": {
                    "message": "Missing required parameters!"
                },
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    # check did discord id is exist in users table (please check by database)
    # Please use app.state.services.database.fetch_val
    user_info = await app.state.services.database.fetch_val(
        "SELECT * FROM users WHERE discord_id = :discord_id",
        {"discord_id": discord_id},
    )
    print(user_info)
    if not user_info:
        # 404 not found, response = "User not found!"
        return ORJSONResponse(
            {
                "success": False,
                "response": {
                    "message": "User not found!"
                },
            },
            status_code=status.HTTP_404_NOT_FOUND,
        )
    user_info = await players_repo.fetch_one(id=user_info)

    if not user_info["priv"] & Privileges.NAT:
        # 403 forbidden, response = "You are not a NAT!"
        return ORJSONResponse(
            {
                "success": False,
                "response": {
                    "message": "You are not a NAT!"
                },
            },
            status_code=status.HTTP_403_FORBIDDEN,
        )

    if key != app.settings.OSU_API_KEY:
        # Sucess = false, 403 forbidden, response = "Invalid osu!api key"
        return ORJSONResponse(
            {
                "success": False,
                "response": {
                    "message": "Invalid osu!api key!"
                },
            },
            status_code=status.HTTP_403_FORBIDDEN,
        )
    # Check did beatmap set id is already ranked or not
    # Please use app.state.services.database.fetch_val
    # If status = 2, that mean beatmap is already ranked
    # If status = 4, that mean beatmap is already qualified
    # We will vote only if status is 0 or 1
    beatmap_info = await app.state.services.database.fetch_val(
        "SELECT status FROM maps WHERE set_id = :set_id",
        {"set_id": set_id},
    )
    if beatmap_info in (2, 4, 5):
        # status is true but response is already ranked, 403 forbidden
        # do like success but do not include vote count and need count
        return ORJSONResponse(
            {
                "success": False,
                "response": {
                    "message": "Beatmap is already ranked, or qualified!"
                },
            },
            status_code=status.HTTP_403_FORBIDDEN,
        )
    else:
        # Check did beatmap set id is exist in database?
        # Please use app.state.services.database.fetch_val
        # If not exist, return error
        # If exist, continue
        beatmap_info = await app.state.services.database.fetch_val(
            "SELECT set_id FROM maps WHERE set_id = :set_id",
            {"set_id": set_id},
        )
        if not beatmap_info:
            # sucess = false, 404 not found, response = "Beatmap not found"
            return ORJSONResponse(
                {
                    "success": False,
                    "response": {
                        "message": "Beatmap not found! (Please recheck your beatmap set_id!)"
                    },
                },
                status_code=status.HTTP_404_NOT_FOUND,
            )
        # Loveable beatmap, edit value in database to 2
        print(set_id)
        await app.state.services.database.execute(
            "UPDATE maps SET status = 2 WHERE set_id = :set_id",
            {"set_id": set_id},
        )
        print("Ranked")
        # set beatmap change_date to current time
        await app.state.services.database.execute(
            "UPDATE maps SET change_date = now() WHERE set_id = :set_id",
            {"set_id": set_id}
        )
        # Get beatmap id, not set id
        ids = await app.state.services.database.fetch_all(
        "SELECT id FROM maps WHERE set_id = :set_id",
        {"set_id": set_id}
        )
        for idmap in ids:
            idmap = idmap["id"]
            print(idmap)
            md5 = await app.state.services.database.fetch_val(
                "SELECT md5 FROM maps WHERE id = :id",
                {"id": idmap}
            )
            # update map by map_id
            await maps_repo.update(idmap, status=2, frozen=True)
            if md5 in app.state.cache.beatmap:
                app.state.cache.beatmap[md5].status = 2
                app.state.cache.beatmap[md5].frozen = True
            # delete request from map_requests (map_id)
            await app.state.services.database.execute(
                "DELETE FROM map_requests WHERE map_id = :id",
                {"id": idmap}
            )
        # Send to discord webhook
        # Getting beatmap info by database
        beatmap_info = await app.state.services.database.fetch_one(
            "SELECT * FROM maps WHERE set_id = :set_id",
            {"set_id": set_id},
        )
        # Send to discord webhook nomination
        # We need artist, title, version, creator, and set_id
        artist = beatmap_info["artist"]
        title = beatmap_info["title"]
        creator = beatmap_info["creator"]
        # Get username of user
        username = user_info["name"]
        if webhook_url := app.settings.DISCORD_NOMINATION_WEBHOOK:
            embed = Embed(title="", description=f"[{artist} - {title} ({creator})](https://osu.ppy.sh/beatmapsets/{set_id}) is now ranked! (by {username})", timestamp=datetime.datetime.utcnow(), color=52478)
            embed.set_author(name="Automatic Status Bot (Click to get beatmap!)", icon_url="https://a.ppy.sh/1", url=f"https://osu.ppy.sh/beatmapsets/{set_id}")
            embed.set_image(url=f"https://assets.ppy.sh/beatmaps/{set_id}/covers/card.jpg")
            embed.set_footer(text="Nomination Tools")
            # Blue color
            embed.color = 0x0000FF
            webhook = Webhook(webhook_url, embeds=[embed])
        await webhook.post()
        return ORJSONResponse(
            {
                "success": True,
                "response": {
                    "message": "Now, this mapset has been ranked!",
                },
            },
            status_code=status.HTTP_200_OK,
        )
# Give me example of api
# /rank_beatmap?discord_id=736163902835916880&set_id=772

# /cancel_beatmap, only for NAT, need discord id and set id and osu!api key that match with config
# cancel beatmap is for cancel beatmap it going qualified in soon, by deleting all beatmapset id in database
# if beatmap is already love, return error, if beatmap is already ranked, return error
@router.get("/cancel_beatmap")
async def api_cancel_beatmap(
    discord_id: int | None = Query(None, alias="discord_id", ge=100000000000000000, le=999999999999999999),
    set_id: int | None = Query(None, alias="set_id", ge=0, le=2_147_483_647),
    key: str | None = Query(None, alias="key", min_length=1, max_length=64),
) -> Response:
    # Print discord_id, set_id, and key
    print(discord_id, set_id, key)
    if not discord_id or not set_id or not key:
        # 400 bad request, response = "Missing required parameters!"
        return ORJSONResponse(
            {
                "success": False,
                "response": {
                    "message": "Missing required parameters!"
                },
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    # check did discord id is exist in users table (please check by database)
    # Please use app.state.services.database.fetch_val
    user_info = await app.state.services.database.fetch_val(
        "SELECT * FROM users WHERE discord_id = :discord_id",
        {"discord_id": discord_id},
    )
    print(user_info)
    if not user_info:
        # 404 not found, response = "User not found!"
        return ORJSONResponse(
            {
                "success": False,
                "response": {
                    "message": "User not found!"
                },
            },
            status_code=status.HTTP_404_NOT_FOUND,
        )
    user_info = await players_repo.fetch_one(id=user_info)

    if not user_info["priv"] & Privileges.NAT:
        # 403 forbidden, response = "You are not a NAT!"
        return ORJSONResponse(
            {
                "success": False,
                "response": {
                    "message": "You are not a NAT!"
                },
            },
            status_code=status.HTTP_403_FORBIDDEN,
        )

    if key != app.settings.OSU_API_KEY:
        # Sucess = false, 403 forbidden, response = "Invalid osu!api key"
        return ORJSONResponse(
            {
                "success": False,
                "response": {
                    "message": "Invalid osu!api key!"
                },
            },
            status_code=status.HTTP_403_FORBIDDEN,
        )
    # Check did beatmap set id is already ranked or not
    # Please use app.state.services.database.fetch_val
    # If status = 2, that mean beatmap is already ranked
    # If status = 3, that mean beatmap is already loved
    # If status = 4, that mean beatmap is already qualified
    # We will vote only if status is 0 or 1
    beatmap_info = await app.state.services.database.fetch_val(
        "SELECT status FROM maps WHERE set_id = :set_id",
        {"set_id": set_id},
    )
    if beatmap_info in (2, 3, 5):
        # status is true but response is already ranked, 403 forbidden
        # do like success but do not include vote count and need count
        return ORJSONResponse(
            {
                "success": False,
                "response": {
                    "message": "Beatmap is already ranked, loved, or qualified!"
                },
            },
            status_code=status.HTTP_403_FORBIDDEN,
        )
    else:
        # Check did beatmap set id is exist in database?
        # Please use app.state.services.database.fetch_val
        # If not exist, return error
        # If exist, continue
        beatmap_info = await app.state.services.database.fetch_val(
            "SELECT set_id FROM maps WHERE set_id = :set_id",
            {"set_id": set_id},
        )
        if not beatmap_info:
            # sucess = false, 404 not found, response = "Beatmap not found"
            return ORJSONResponse(
                {
                    "success": False,
                    "response": {
                        "message": "Beatmap not found! (Please recheck your beatmap set_id!)"
                    },
                },
                status_code=status.HTTP_404_NOT_FOUND,
            )
        # Loveable beatmap, edit value in database to 0
        print(set_id)
        await app.state.services.database.execute(
            "UPDATE maps SET status = 0 WHERE set_id = :set_id",
            {"set_id": set_id},
        )
        print("Canceled")
        # set beatmap change_date to current time
        await app.state.services.database.execute(
            "UPDATE maps SET change_date = now() WHERE set_id = :set_id",
            {"set_id": set_id}
        )
        # Get beatmap id, not set id
        ids = await app.state.services.database.fetch_all(
        "SELECT id FROM maps WHERE set_id = :set_id",
        {"set_id": set_id}
        )
        for idmap in ids:
            idmap = idmap["id"]
            print(idmap)
            md5 = await app.state.services.database.fetch_val(
                "SELECT md5 FROM maps WHERE id = :id",
                {"id": idmap}
            )
            # update map by map_id
            await maps_repo.update(idmap, status=0, frozen=False)
            if md5 in app.state.cache.beatmap:
                app.state.cache.beatmap[md5].status = 0
                app.state.cache.beatmap[md5].frozen = False
            # delete request from map_requests (map_id)
            await app.state.services.database.execute(
                "DELETE FROM map_requests WHERE map_id = :id",
                {"id": idmap}
            )
        # Send to discord webhook
        # Getting beatmap info by database
        beatmap_info = await app.state.services.database.fetch_one(
            "SELECT * FROM maps WHERE set_id = :set_id",
            {"set_id": set_id},
        )
        # Send to discord webhook nomination
        # We need artist, title, version, creator, and set_id
        artist = beatmap_info["artist"]
        title = beatmap_info["title"]
        creator = beatmap_info["creator"]
        # Get username of user
        username = user_info["name"]
        if webhook_url := app.settings.DISCORD_NOMINATION_WEBHOOK:
            embed = Embed(title="", description=f"[{artist} - {title} ({creator})](https://osu.ppy.sh/beatmapsets/{set_id}) is now canceled! (by {username})", timestamp=datetime.datetime.utcnow(), color=52478)
            embed.set_author(name="Automatic Status Bot (Click to get beatmap!)", icon_url="https://a.ppy.sh/1", url=f"https://osu.ppy.sh/beatmapsets/{set_id}")
            embed.set_image(url=f"https://assets.ppy.sh/beatmaps/{set_id}/covers/card.jpg")
            embed.set_footer(text="Nomination Tools")
            # Red color
            embed.color = 0xFF0000
            webhook = Webhook(webhook_url, embeds=[embed])
        await webhook.post()
        # Send to discord webhook qualification
        if webhook_url := app.settings.DISCORD_QUALIFIED_WEBHOOK:
            embed = Embed(title="", description=f"[{artist} - {title} ({creator})](https://osu.ppy.sh/beatmapsets/{set_id}) is now canceled!", timestamp=datetime.datetime.utcnow(), color=52478)
            embed.set_author(name="Automatic Status Bot (Click to get beatmap!)", icon_url="https://a.ppy.sh/1", url=f"https://osu.ppy.sh/beatmapsets/{set_id}")
            embed.set_image(url=f"https://assets.ppy.sh/beatmaps/{set_id}/covers/card.jpg")
            embed.set_footer(text="Nomination Tools")
            # Red color
            embed.color = 0xFF0000
            webhook = Webhook(webhook_url, embeds=[embed])
        await webhook.post()
        return ORJSONResponse(
            {
                "success": True,
                "response": {
                    "message": "Now, this mapset has been canceled!",
                },
            },
            status_code=status.HTTP_200_OK,
        )
# Give me example of api
# /cancel_beatmap?discord_id=736163902835916880&set_id=772

# /restrict_player, only for Staff, need discord id and osu!api key that match with config
# restrict player is for restrict player from playing, if player is already restricted, return error
# discord_id for staff, player name is target player to restrict, and reason is reason to restrict player
# target = await app.state.sessions.players.from_cache_or_sql(name=username)
@router.get("/restrict_player")
async def api_restrict_player(
    discord_id: int | None = Query(None, alias="discord_id", ge=100000000000000000, le=999999999999999999),
    username: str | None = Query(None, alias="username", pattern=regexes.USERNAME.pattern),
    reason: str | None = Query(None, alias="reason", min_length=1, max_length=128),
    key: str | None = Query(None, alias="key", min_length=1, max_length=64),
) -> Response:
    # Print discord_id, username, reason, and key
    print(discord_id, username, reason, key)
    if not discord_id or not username or not reason or not key:
        # 400 bad request, response = "Missing required parameters!"
        return ORJSONResponse(
            {
                "success": False,
                "response": {
                    "message": "Missing required parameters!"
                },
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    # check did discord id is exist in users table (please check by database)
    # Please use app.state.services.database.fetch_val
    user_info = await app.state.services.database.fetch_val(
        "SELECT * FROM users WHERE discord_id = :discord_id",
        {"discord_id": discord_id},
    )
    print(user_info)
    if not user_info:
        # 404 not found, response = "User not found!"
        return ORJSONResponse(
            {
                "success": False,
                "response": {
                    "message": "User not found!"
                },
            },
            status_code=status.HTTP_404_NOT_FOUND,
        )
    user_info = await players_repo.fetch_one(id=user_info)

    if not user_info["priv"] & Privileges.STAFF:
        # 403 forbidden, response = "You are not a Staff!"
        return ORJSONResponse(
            {
                "success": False,
                "response": {
                    "message": "You are not a Staff!"
                },
            },
            status_code=status.HTTP_403_FORBIDDEN,
        )

    if key != app.settings.OSU_API_KEY:
        # Sucess = false, 403 forbidden, response = "Invalid osu!api key"
        return ORJSONResponse(
            {
                "success": False,
                "response": {
                    "message": "Invalid osu!api key!"
                },
            },
            status_code=status.HTTP_403_FORBIDDEN,
        )
    # Check did player is already restricted or not
    # Please use app.state.services.database.fetch_val
    # If status = 0, that mean player is not restricted
    # If status = 1, that mean player is restricted
    # We will restrict only if status is 0
    target = await app.state.sessions.players.from_cache_or_sql(name=username)
    if not target:
        return ORJSONResponse(
            {
                "success": False,
                "response": {
                    "message": "Player not found!"
                },
            },
            status_code=status.HTTP_404_NOT_FOUND,
        )
    # If user is not developer and trying to restrict staff or developer, return error
    if target.priv & Privileges.STAFF or target.priv & Privileges.DEVELOPER:
        return ORJSONResponse(
            {
                "success": False,
                "response": {
                    "message": "You can't restrict staff or developer!"
                },
            },
            status_code=status.HTTP_403_FORBIDDEN,
        )
    # If user is not developer and trying to restrict themselves, return error
    if target.id == user_info["id"]:
        return ORJSONResponse(
            {
                "success": False,
                "response": {
                    "message": "You can't restrict yourself!"
                },
            },
            status_code=status.HTTP_403_FORBIDDEN,
        )
    # If target is already restricted, return error
    if target.restricted:
        return ORJSONResponse(
            {
                "success": False,
                "response": {
                    "message": "Player is already restricted!"
                },
            },
            status_code=status.HTTP_403_FORBIDDEN,
        )
    # Restrict player
    # Getting admin info
    admin = await app.state.sessions.players.from_cache_or_sql(name=user_info["name"])
    await target.restrict(admin=admin, reason=reason)
    # refresh thier client state
    if target.is_online:
        target.logout()
    return ORJSONResponse(
        {
            "success": True,
            "response": {
                "message": "Player has been restricted!",
                "reason" : reason
            },
        },
        status_code=status.HTTP_200_OK,
    )
# Give me example of api
# /restrict_player?discord_id=736163902835916880&username=Koi&reason=Bad%20player      

#/unrestrict_player, only for Staff, need discord id and osu!api key that match with config
# same with restrict player, but this is for unrestrict player
@router.get("/unrestrict_player")
async def api_unrestrict_player(
    discord_id: int | None = Query(None, alias="discord_id", ge=100000000000000000, le=999999999999999999),
    username: str | None = Query(None, alias="username", pattern=regexes.USERNAME.pattern),
    reason: str | None = Query(None, alias="reason", min_length=1, max_length=128),
    key: str | None = Query(None, alias="key", min_length=1, max_length=64),
) -> Response:
    # Print discord_id, username, reason, and key
    print(discord_id, username, reason, key)
    if not discord_id or not username or not reason or not key:
        # 400 bad request, response = "Missing required parameters!"
        return ORJSONResponse(
            {
                "success": False,
                "response": {
                    "message": "Missing required parameters!"
                },
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    # check did discord id is exist in users table (please check by database)
    # Please use app.state.services.database.fetch_val
    user_info = await app.state.services.database.fetch_val(
        "SELECT * FROM users WHERE discord_id = :discord_id",
        {"discord_id": discord_id},
    )
    print(user_info)
    if not user_info:
        # 404 not found, response = "User not found!"
        return ORJSONResponse(
            {
                "success": False,
                "response": {
                    "message": "User not found!"
                },
            },
            status_code=status.HTTP_404_NOT_FOUND,
        )
    user_info = await players_repo.fetch_one(id=user_info)

    if not user_info["priv"] & Privileges.STAFF:
        # 403 forbidden, response = "You are not a Staff!"
        return ORJSONResponse(
            {
                "success": False,
                "response": {
                    "message": "You are not a Staff!"
                },
            },
            status_code=status.HTTP_403_FORBIDDEN,
        )

    if key != app.settings.OSU_API_KEY:
        # Sucess = false, 403 forbidden, response = "Invalid osu!api key"
        return ORJSONResponse(
            {
                "success": False,
                "response": {
                    "message": "Invalid osu!api key!"
                },
            },
            status_code=status.HTTP_403_FORBIDDEN,
        )
    # Check did player is already restricted or not
    # Please use app.state.services.database.fetch_val
    # If status = 0, that mean player is not restricted
    # If status = 1, that mean player is restricted
    # We will restrict only if status is 0
    target = await app.state.sessions.players.from_cache_or_sql(name=username)
    if not target:
        return ORJSONResponse(
            {
                "success": False,
                "response": {
                    "message": "Player not found!"
                },
            },
            status_code=status.HTTP_404_NOT_FOUND,
        )
    # If target player is not restricted, return error
    if not target.restricted:
        return ORJSONResponse(
            {
                "success": False,
                "response": {
                    "message": "Player is not restricted!"
                },
            },
            status_code=status.HTTP_403_FORBIDDEN,
        )
    # Unrestrict player
    # Getting admin info
    admin = await app.state.sessions.players.from_cache_or_sql(name=user_info["name"])
    await target.unrestrict(admin=admin, reason=reason)
    return ORJSONResponse(
        {
            "success": True,
            "response": {
                "message": "Player has been unrestricted!",
                "reason" : reason
            },
        },
        status_code=status.HTTP_200_OK,
    )
# Give me example of api
# /unrestrict_player?discord_id=736163902835916880&username=Koi&reason=Bad%20player

#/whitelist_player, only for Staff, need discord id and osu!api key that match with config
# We need only discord_id of staff, username of player, osu!api key to verify, it is, no any other parameters need
# If player is already whitelisted, return error
@router.get("/whitelist_player")
async def api_whitelist_player(
    discord_id: int | None = Query(None, alias="discord_id", ge=100000000000000000, le=999999999999999999),
    username: str | None = Query(None, alias="username", pattern=regexes.USERNAME.pattern),
    key: str | None = Query(None, alias="key", min_length=1, max_length=64),
) -> Response:
    # Print discord_id, username, and key
    print(discord_id, username, key)
    if not discord_id or not username or not key:
        # 400 bad request, response = "Missing required parameters!"
        return ORJSONResponse(
            {
                "success": False,
                "response": {
                    "message": "Missing required parameters!"
                },
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    # check did discord id is exist in users table (please check by database)
    # Please use app.state.services.database.fetch_val
    user_info = await app.state.services.database.fetch_val(
        "SELECT * FROM users WHERE discord_id = :discord_id",
        {"discord_id": discord_id},
    )
    print(user_info)
    if not user_info:
        # 404 not found, response = "User not found!"
        return ORJSONResponse(
            {
                "success": False,
                "response": {
                    "message": "User not found!"
                },
            },
            status_code=status.HTTP_404_NOT_FOUND,
        )
    user_info = await players_repo.fetch_one(id=user_info)

    if not user_info["priv"] & Privileges.STAFF:
        # 403 forbidden, response = "You are not a Staff!"
        return ORJSONResponse(
            {
                "success": False,
                "response": {
                    "message": "You are not a Staff!"
                },
            },
            status_code=status.HTTP_403_FORBIDDEN,
        )

    if key != app.settings.OSU_API_KEY:
        # Sucess = false, 403 forbidden, response = "Invalid osu!api key"
        return ORJSONResponse(
            {
                "success": False,
                "response": {
                    "message": "Invalid osu!api key!"
                },
            },
            status_code=status.HTTP_403_FORBIDDEN,
        )
    # Check did player is already whitelisted or not
    # Please use app.state.services.database.fetch_val
    # If status = 0, that mean player is not whitelisted
    # If status = 1, that mean player is whitelisted
    # We will whitelist only if status is 0
    target = await app.state.sessions.players.from_cache_or_sql(name=username)
    if not target:
        return ORJSONResponse(
            {
                "success": False,
                "response": {
                    "message": "Player not found!"
                },
            },
            status_code=status.HTTP_404_NOT_FOUND,
        )
    # If target player is already whitelisted, return error
    if target.priv & Privileges.WHITELISTED:
        return ORJSONResponse(
            {
                "success": False,
                "response": {
                    "message": "Player is already whitelisted!"
                },
            },
            status_code=status.HTTP_403_FORBIDDEN,
        )
    # Whitelist player
    # Getting admin info
    await target.add_privs(Privileges.WHITELISTED)
    # Return success
    return ORJSONResponse(
        {
            "success": True,
            "response": {
                "message": "Player has been whitelisted!"
            },
        },
        status_code=status.HTTP_200_OK,
    )
# Give me example of api
# /whitelist_player?discord_id=736163902835916880&username=Koi

# Can you tell me, which api we add for serveal days? list please
# /love_beatmap, /rank_beatmap, /cancel_beatmap, /restrict_player, /unrestrict_player, /whitelist_player   
@router.get("/get_player_status")
async def api_get_player_status(
    user_id: int | None = Query(None, alias="id", ge=3, le=2_147_483_647),
    username: str | None = Query(None, alias="name", pattern=regexes.USERNAME.pattern),
) -> Response:
    """Return a players current status, if they are online."""
    if username and user_id:
        return ORJSONResponse(
            {"status": "Must provide either id OR name!"},
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    if username:
        player = app.state.sessions.players.get(name=username)
    elif user_id:
        player = app.state.sessions.players.get(id=user_id)
    else:
        return ORJSONResponse(
            {"status": "Must provide either id OR name!"},
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    if not player:
        # no such player online, return their last seen time if they exist in sql

        if username:
            row = await players_repo.fetch_one(name=username)
        else:  # if userid
            row = await players_repo.fetch_one(id=user_id)

        if not row:
            return ORJSONResponse(
                {"status": "Player not found."},
                status_code=status.HTTP_404_NOT_FOUND,
            )

        return ORJSONResponse(
            {
                "status": "success",
                "player_status": {
                    "online": False,
                    "last_seen": row["latest_activity"],
                },
            },
        )

    if player.status.map_md5:
        bmap = await Beatmap.from_md5(player.status.map_md5)
    else:
        bmap = None

    return ORJSONResponse(
        {
            "status": "success",
            "player_status": {
                "online": True,
                "login_time": player.login_time,
                "status": {
                    "action": int(player.status.action),
                    "info_text": player.status.info_text,
                    "mode": int(player.status.mode),
                    "mods": int(player.status.mods),
                    "beatmap": bmap.as_dict if bmap else None,
                },
            },
        },
    )


@router.get("/get_player_scores")
async def api_get_player_scores(
    scope: Literal["recent", "best", "first"],
    user_id: int | None = Query(None, alias="id", ge=3, le=2_147_483_647),
    username: str | None = Query(None, alias="name", pattern=regexes.USERNAME.pattern),
    mods_arg: str | None = Query(None, alias="mods"),
    mode_arg: int = Query(0, alias="mode", ge=0, le=11),
    limit: int = Query(25, ge=1, le=100),
    include_loved: bool = False,
    include_failed: bool = True,
) -> Response:
    """Return a list of a given user's recent/best/first scores."""
    if mode_arg in (
        GameMode.RELAX_MANIA,
        GameMode.AUTOPILOT_CATCH,
        GameMode.AUTOPILOT_TAIKO,
        GameMode.AUTOPILOT_MANIA,
    ):
        return ORJSONResponse(
            {"status": "Invalid gamemode."},
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    if username and user_id:
        return ORJSONResponse(
            {"status": "Must provide either id OR name!"},
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    if username:
        player = await app.state.sessions.players.from_cache_or_sql(name=username)
    elif user_id:
        player = await app.state.sessions.players.from_cache_or_sql(id=user_id)
    else:
        return ORJSONResponse(
            {"status": "Must provide either id OR name!"},
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    if not player:
        return ORJSONResponse(
            {"status": "Player not found."},
            status_code=status.HTTP_404_NOT_FOUND,
        )

    # parse args (scope, mode, mods, limit)

    mode = GameMode(mode_arg)

    strong_equality = True
    if mods_arg is not None:
        if mods_arg[0] in ("~", "="):  # weak/strong equality
            strong_equality = mods_arg[0] == "="
            mods_arg = mods_arg[1:]

        if mods_arg.isdecimal():
            # parse from int form
            mods = Mods(int(mods_arg))
        else:
            # parse from string form
            mods = Mods.from_modstr(mods_arg)
    else:
        mods = None

    # build sql query & fetch info

    query = [
        "SELECT t.id, t.map_md5, t.score, t.pp, t.acc, t.max_combo, "
        "t.mods, t.n300, t.n100, t.n50, t.nmiss, t.ngeki, t.nkatu, t.grade, "
        "t.status, t.mode, t.play_time, t.time_elapsed, t.perfect "
        "FROM scores t "
        "INNER JOIN maps b ON t.map_md5 = b.md5 "
        "WHERE t.userid = :user_id AND t.mode = :mode",
    ]

    params: dict[str, object] = {
        "user_id": player.id,
        "mode": mode,
    }

    if mods is not None:
        if strong_equality:
            query.append("AND t.mods & :mods = :mods")
        else:
            query.append("AND t.mods & :mods != 0")

        params["mods"] = mods

    if scope == "best":
        allowed_statuses = [2, 3]

        if include_loved:
            allowed_statuses.append(5)

        query.append("AND t.status = 2 AND b.status IN :statuses")
        params["statuses"] = allowed_statuses
        sort = "t.pp"
    elif scope == "recent":
        if not include_failed:
            query.append("AND t.status != 0")

        sort = "t.play_time"
    else: # "first"
        lb_sort = "score" if mode_arg <= 3 else "pp" # vanilla goes by score, relax and ap by pp
        query = [
            "SELECT t.id, t.map_md5, t.score, t.pp, t.acc, t.max_combo, "
            "t.mods, t.n300, t.n100, t.n50, t.nmiss, t.ngeki, t.nkatu, t.grade, "
            "t.status, t.mode, t.time_elapsed, t.play_time, t.perfect "
            "FROM scores t "
           f"JOIN (SELECT map_md5, MAX({lb_sort}) AS points FROM scores WHERE status = 2 GROUP BY map_md5) max_scores "
           f"ON t.map_md5 = max_scores.map_md5 AND t.{lb_sort} = max_scores.points "
            "INNER JOIN maps b ON max_scores.map_md5 = b.md5 "
            "WHERE t.userid = :user_id AND t.mode = :mode AND t.status = 2 AND b.status IN (2, 3, 5)"
        ]
        sort = "t.play_time"

    query.append(f"ORDER BY {sort} DESC LIMIT :limit")
    params["limit"] = limit

    rows = [
        dict(row)
        for row in await app.state.services.database.fetch_all(" ".join(query), params)
    ]

    # fetch & return info from sql
    for row in rows:
        mods = Mods(row["mods"])
        bmap = await Beatmap.from_md5(row.pop("map_md5"))
        row["beatmap"] = bmap.as_dict if bmap else None
        row["mods_readable"] = mods.__repr__()

    player_info = {
        "id": player.id,
        "name": player.name,
        "clan": {
            "id": player.clan.id,
            "name": player.clan.name,
            "tag": player.clan.tag,
        }
        if player.clan
        else None,
    }

    return ORJSONResponse(
        {
            "status": "success",
            "scores": rows,
            "player": player_info,
        },
    )


@router.get("/get_player_most_played")
async def api_get_player_most_played(
    user_id: int | None = Query(None, alias="id", ge=3, le=2_147_483_647),
    username: str | None = Query(None, alias="name", pattern=regexes.USERNAME.pattern),
    mode_arg: int = Query(0, alias="mode", ge=0, le=11),
    limit: int = Query(25, ge=1, le=100),
) -> Response:
    """Return the most played beatmaps of a given player."""
    # NOTE: this will almost certainly not scale well, lol.
    if mode_arg in (
        GameMode.RELAX_MANIA,
        GameMode.AUTOPILOT_CATCH,
        GameMode.AUTOPILOT_TAIKO,
        GameMode.AUTOPILOT_MANIA,
    ):
        return ORJSONResponse(
            {"status": "Invalid gamemode."},
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    if user_id is not None:
        player = await app.state.sessions.players.from_cache_or_sql(id=user_id)
    elif username is not None:
        player = await app.state.sessions.players.from_cache_or_sql(name=username)
    else:
        return ORJSONResponse(
            {"status": "Must provide either id or name."},
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    if not player:
        return ORJSONResponse(
            {"status": "Player not found."},
            status_code=status.HTTP_404_NOT_FOUND,
        )

    # parse args (mode, limit)

    mode = GameMode(mode_arg)

    # fetch & return info from sql
    rows = await app.state.services.database.fetch_all(
        "SELECT m.md5, m.id, m.set_id, m.status, "
        "m.artist, m.title, m.version, m.creator, COUNT(*) plays "
        "FROM scores s "
        "INNER JOIN maps m ON m.md5 = s.map_md5 "
        "WHERE s.userid = :user_id "
        "AND s.mode = :mode "
        "GROUP BY s.map_md5 "
        "ORDER BY plays DESC "
        "LIMIT :limit",
        {"user_id": player.id, "mode": mode, "limit": limit},
    )

    return ORJSONResponse(
        {
            "status": "success",
            "maps": [dict(row) for row in rows],
        },
    )


@router.get("/get_map_info")
async def api_get_map_info(
    map_id: int | None = Query(None, alias="id", ge=3, le=2_147_483_647),
    md5: str | None = Query(None, alias="md5", min_length=32, max_length=32),
) -> Response:
    """Return information about a given beatmap."""
    if map_id is not None:
        bmap = await Beatmap.from_bid(map_id)
    elif md5 is not None:
        bmap = await Beatmap.from_md5(md5)
    else:
        return ORJSONResponse(
            {"status": "Must provide either id or md5!"},
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    if not bmap:
        return ORJSONResponse(
            {"status": "Map not found."},
            status_code=status.HTTP_404_NOT_FOUND,
        )

    return ORJSONResponse(
        {
            "status": "success",
            "map": bmap.as_dict,
        },
    )


@router.get("/get_map_scores")
async def api_get_map_scores(
    scope: Literal["recent", "best"],
    map_id: int | None = Query(None, alias="id", ge=0, le=2_147_483_647),
    map_md5: str | None = Query(None, alias="md5", min_length=32, max_length=32),
    mods_arg: str | None = Query(None, alias="mods"),
    mode_arg: int = Query(0, alias="mode", ge=0, le=11),
    limit: int = Query(50, ge=1, le=100),
) -> Response:
    """Return the top n scores on a given beatmap."""
    if mode_arg in (
        GameMode.RELAX_MANIA,
        GameMode.AUTOPILOT_CATCH,
        GameMode.AUTOPILOT_TAIKO,
        GameMode.AUTOPILOT_MANIA,
    ):
        return ORJSONResponse(
            {"status": "Invalid gamemode."},
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    if map_id is not None:
        bmap = await Beatmap.from_bid(map_id)
    elif map_md5 is not None:
        bmap = await Beatmap.from_md5(map_md5)
    else:
        return ORJSONResponse(
            {"status": "Must provide either id or md5!"},
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    if not bmap:
        return ORJSONResponse(
            {"status": "Map not found."},
            status_code=status.HTTP_404_NOT_FOUND,
        )

    # parse args (scope, mode, mods, limit)

    mode = GameMode(mode_arg)

    strong_equality = True
    if mods_arg is not None:
        if mods_arg[0] in ("~", "="):
            strong_equality = mods_arg[0] == "="
            mods_arg = mods_arg[1:]

        if mods_arg.isdecimal():
            # parse from int form
            mods = Mods(int(mods_arg))
        else:
            # parse from string form
            mods = Mods.from_modstr(mods_arg)
    else:
        mods = None

    # NOTE: userid will eventually become player_id,
    # along with everywhere else in the codebase.
    query = [
        "SELECT s.map_md5, s.id, s.score, s.pp, s.acc, s.max_combo, s.mods, "
        "s.n300, s.n100, s.n50, s.nmiss, s.ngeki, s.nkatu, s.grade, s.status, "
        "s.mode, s.play_time, s.time_elapsed, s.userid, s.perfect, "
        "u.name player_name, u.country, "
        "c.id clan_id, c.name clan_name, c.tag clan_tag "
        "FROM scores s "
        "INNER JOIN users u ON u.id = s.userid "
        "LEFT JOIN clans c ON c.id = u.clan_id "
        "WHERE s.map_md5 = :map_md5 "
        "AND s.mode = :mode "
        "AND s.status = 2 "
        "AND u.priv & 1",
    ]
    params: dict[str, object] = {
        "map_md5": bmap.md5,
        "mode": mode,
    }

    if mods is not None:
        if strong_equality:
            query.append("AND mods & :mods = :mods")
        else:
            query.append("AND mods & :mods != 0")

        params["mods"] = mods

    # unlike /get_player_scores, we'll sort by score/pp depending
    # on the mode played, since we want to replicated leaderboards.
    if scope == "best":
        sort = "pp" if mode >= GameMode.RELAX_OSU else "score"
    else:  # recent
        sort = "play_time"

    query.append(f"ORDER BY {sort} DESC LIMIT :limit")
    params["limit"] = limit

    rows = await app.state.services.database.fetch_all(" ".join(query), params)

    return ORJSONResponse(
        {
            "status": "success",
            "scores": [dict(row) for row in rows],
        },
    )


@router.get("/get_score_info")
async def api_get_score_info(
    score_id: int = Query(..., alias="id", ge=0, le=9_223_372_036_854_775_807),
) -> Response:
    """Return information about a given score."""
    score = await scores_repo.fetch_one(score_id)

    if score is None:
        return ORJSONResponse(
            {"status": "Score not found."},
            status_code=status.HTTP_404_NOT_FOUND,
        )

    return ORJSONResponse({"status": "success", "score": score})


# TODO: perhaps we can do something to make these count towards replay views,
#       but we'll want to make it difficult to spam.
@router.get("/get_replay")
async def api_get_replay(
    score_id: int = Query(..., alias="id", ge=0, le=9_223_372_036_854_775_807),
    include_headers: bool = True,
) -> Response:
    """Return a given replay (including headers)."""
    # fetch replay file & make sure it exists
    replay_file = REPLAYS_PATH / f"{score_id}.osr"
    if not replay_file.exists():
        return ORJSONResponse(
            {"status": "Replay not found."},
            status_code=status.HTTP_404_NOT_FOUND,
        )
    # read replay frames from file
    raw_replay_data = replay_file.read_bytes()
    if not include_headers:
        return Response(
            bytes(raw_replay_data),
            media_type="application/octet-stream",
            headers={
                "Content-Description": "File Transfer",
                # TODO: should we do the query to fetch
                # info for content-disposition for this..?
            },
        )
    # add replay headers from sql
    # TODO: osu_version & life graph in scores tables?
    row = await app.state.services.database.fetch_one(
        "SELECT u.name username, m.md5 map_md5, "
        "m.artist, m.title, m.version, "
        "s.mode, s.n300, s.n100, s.n50, s.ngeki, "
        "s.nkatu, s.nmiss, s.score, s.max_combo, "
        "s.perfect, s.mods, s.play_time "
        "FROM scores s "
        "INNER JOIN users u ON u.id = s.userid "
        "INNER JOIN maps m ON m.md5 = s.map_md5 "
        "WHERE s.id = :score_id",
        {"score_id": score_id},
    )
    if not row:
        # score not found in sql
        return ORJSONResponse(
            {"status": "Score not found."},
            status_code=status.HTTP_404_NOT_FOUND,
        )  # but replay was?
    # generate the replay's hash
    replay_md5 = hashlib.md5(
        "{}p{}o{}o{}t{}a{}r{}e{}y{}o{}u{}{}{}".format(
            row["n100"] + row["n300"],
            row["n50"],
            row["ngeki"],
            row["nkatu"],
            row["nmiss"],
            row["map_md5"],
            row["max_combo"],
            str(row["perfect"] == 1),
            row["username"],
            row["score"],
            0,  # TODO: rank
            row["mods"],
            "True",  # TODO: ??
        ).encode(),
    ).hexdigest()
    # create a buffer to construct the replay output
    replay_data = bytearray()
    # pack first section of headers.
    replay_data += struct.pack(
        "<Bi",
        GameMode(row["mode"]).as_vanilla,
        20200207,
    )  # TODO: osuver
    replay_data += app.packets.write_string(row["map_md5"])
    replay_data += app.packets.write_string(row["username"])
    replay_data += app.packets.write_string(replay_md5)
    replay_data += struct.pack(
        "<hhhhhhihBi",
        row["n300"],
        row["n100"],
        row["n50"],
        row["ngeki"],
        row["nkatu"],
        row["nmiss"],
        row["score"],
        row["max_combo"],
        row["perfect"],
        row["mods"],
    )
    replay_data += b"\x00"  # TODO: hp graph
    timestamp = int(row["play_time"].timestamp() * 1e7)
    replay_data += struct.pack("<q", timestamp + DATETIME_OFFSET)
    # pack the raw replay data into the buffer
    replay_data += struct.pack("<i", len(raw_replay_data))
    replay_data += raw_replay_data
    # pack additional info buffer.
    replay_data += struct.pack("<q", score_id)
    # NOTE: target practice sends extra mods, but
    # can't submit scores so should not be a problem.
    # stream data back to the client
    return Response(
        bytes(replay_data),
        media_type="application/octet-stream",
        headers={
            "Content-Description": "File Transfer",
            "Content-Disposition": (
                'attachment; filename="{username} - '
                "{artist} - {title} [{version}] "
                '({play_time:%Y-%m-%d}).osr"'
            ).format(**dict(row._mapping)),
        },
    )


@router.get("/get_match")
async def api_get_match(
    match_id: int = Query(..., alias="id", ge=1, le=64),
) -> Response:
    """Return information of a given multiplayer match."""
    # TODO: eventually, this should contain recent score info.

    match = app.state.sessions.matches[match_id]
    if not match:
        return ORJSONResponse(
            {"status": "Match not found."},
            status_code=status.HTTP_404_NOT_FOUND,
        )

    return ORJSONResponse(
        {
            "status": "success",
            "match": {
                "name": match.name,
                "mode": match.mode,
                "mods": int(match.mods),
                "seed": match.seed,
                "host": {"id": match.host.id, "name": match.host.name},
                "refs": [
                    {"id": player.id, "name": player.name} for player in match.refs
                ],
                "in_progress": match.in_progress,
                "is_scrimming": match.is_scrimming,
                "map": {
                    "id": match.map_id,
                    "md5": match.map_md5,
                    "name": match.map_name,
                },
                "active_slots": {
                    str(idx): {
                        "loaded": slot.loaded,
                        "mods": int(slot.mods),
                        "player": {"id": slot.player.id, "name": slot.player.name},
                        "skipped": slot.skipped,
                        "status": int(slot.status),
                        "team": int(slot.team),
                    }
                    for idx, slot in enumerate(match.slots)
                    if slot.player
                },
            },
        },
    )


@router.get("/get_leaderboard")
async def api_get_global_leaderboard(
    sort: Literal["tscore", "rscore", "pp", "acc", "plays", "playtime"] = "pp",
    mode_arg: int = Query(0, alias="mode", ge=0, le=11),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, min=0, max=2_147_483_647),
    country: str | None = Query(None, min_length=2, max_length=2),
) -> Response:
    if mode_arg in (
        GameMode.RELAX_MANIA,
        GameMode.AUTOPILOT_CATCH,
        GameMode.AUTOPILOT_TAIKO,
        GameMode.AUTOPILOT_MANIA,
    ):
        return ORJSONResponse(
            {"status": "Invalid gamemode."},
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    mode = GameMode(mode_arg)

    query_conditions = ["s.mode = :mode", "u.priv & 1", f"s.{sort} > 0"]
    query_parameters: dict[str, object] = {"mode": mode}

    if country is not None:
        query_conditions.append("u.country = :country")
        query_parameters["country"] = country

    rows = await app.state.services.database.fetch_all(
        "SELECT u.id as player_id, u.name, u.country, s.tscore, s.rscore, "
        "s.pp, s.plays, s.playtime, s.acc, s.max_combo, "
        "s.xh_count, s.x_count, s.sh_count, s.s_count, s.a_count, "
        "c.id as clan_id, c.name as clan_name, c.tag as clan_tag "
        "FROM stats s "
        "LEFT JOIN users u USING (id) "
        "LEFT JOIN clans c ON u.clan_id = c.id "
        f"WHERE {' AND '.join(query_conditions)} "
        f"ORDER BY s.{sort} DESC LIMIT :offset, :limit",
        query_parameters | {"offset": offset, "limit": limit},
    )

    return ORJSONResponse(
        {"status": "success", "leaderboard": [dict(row) for row in rows]},
    )


@router.get("/get_clan")
async def api_get_clan(
    clan_id: int = Query(..., alias="id", ge=1, le=2_147_483_647),
) -> Response:
    """Return information of a given clan."""

    # TODO: fetching by name & tag (requires safe_name, safe_tag)

    clan = app.state.sessions.clans.get(id=clan_id)
    if not clan:
        return ORJSONResponse(
            {"status": "Clan not found."},
            status_code=status.HTTP_404_NOT_FOUND,
        )

    members: list[Player] = []

    for member_id in clan.member_ids:
        member = await app.state.sessions.players.from_cache_or_sql(id=member_id)
        assert member is not None
        members.append(member)

    owner = await app.state.sessions.players.from_cache_or_sql(id=clan.owner_id)
    assert owner is not None

    return ORJSONResponse(
        {
            "id": clan.id,
            "name": clan.name,
            "tag": clan.tag,
            "members": [
                {
                    "id": member.id,
                    "name": member.name,
                    "country": member.geoloc["country"]["acronym"],
                    "rank": ("Member", "Officer", "Owner")[member.clan_priv - 1],  # type: ignore
                }
                for member in members
            ],
            "owner": {
                "id": owner.id,
                "name": owner.name,
                "country": owner.geoloc["country"]["acronym"],
                "rank": "Owner",
            },
        },
    )


@router.get("/get_mappool")
async def api_get_pool(
    pool_id: int = Query(..., alias="id", ge=1, le=2_147_483_647),
) -> Response:
    """Return information of a given mappool."""

    # TODO: fetching by name (requires safe_name)

    pool = app.state.sessions.pools.get(id=pool_id)
    if not pool:
        return ORJSONResponse(
            {"status": "Pool not found."},
            status_code=status.HTTP_404_NOT_FOUND,
        )

    return ORJSONResponse(
        {
            "id": pool.id,
            "name": pool.name,
            "created_at": pool.created_at,
            "created_by": format_player_basic(pool.created_by),
            "maps": {
                f"{mods!r}{slot}": format_map_basic(bmap)
                for (mods, slot), bmap in pool.maps.items()
            },
        },
    )


# def requires_api_key(f: Callable) -> Callable:
#     @wraps(f)
#     async def wrapper(conn: Connection) -> HTTPResponse:
#         conn.resp_headers["Content-Type"] = "application/json"
#         if "Authorization" not in conn.headers:
#             return (400, JSON({"status": "Must provide authorization token."}))

#         api_key = conn.headers["Authorization"]

#         if api_key not in app.state.sessions.api_keys:
#             return (401, JSON({"status": "Unknown authorization token."}))

#         # get player from api token
#         player_id = app.state.sessions.api_keys[api_key]
#         player = await app.state.sessions.players.from_cache_or_sql(id=player_id)

#         return await f(conn, player)

#     return wrapper


# NOTE: `Content-Type = application/json` is applied in the above decorator
#                                         for the following api handlers.


# @domain.route("/set_avatar", methods=["POST", "PUT"])
# @requires_api_key
# async def api_set_avatar(conn: Connection, player: Player) -> HTTPResponse:
#     """Update the tokenholder's avatar to a given file."""
#     if "avatar" not in conn.files:
#         return (400, JSON({"status": "must provide avatar file."}))

#     ava_file = conn.files["avatar"]

#     # block files over 4MB
#     if len(ava_file) > (4 * 1024 * 1024):
#         return (400, JSON({"status": "avatar file too large (max 4MB)."}))

#     if ava_file[6:10] in (b"JFIF", b"Exif"):
#         ext = "jpeg"
#     elif ava_file.startswith(b"\211PNG\r\n\032\n"):
#         ext = "png"
#     else:
#         return (400, JSON({"status": "invalid file type."}))

#     # write to the avatar file
#     (AVATARS_PATH / f"{player.id}.{ext}").write_bytes(ava_file)
#     return JSON({"status": "success."})
