from __future__ import annotations

import asyncio
import time

import app.packets
import app.settings
import app.state
from app.constants.privileges import Privileges
from app.logging import Ansi
from app.logging import log
from datetime import datetime, timedelta
from app.discord import Webhook, Embed

OSU_CLIENT_MIN_PING_INTERVAL = 300000 // 1000  # defined by osu!


async def initialize_housekeeping_tasks() -> None:
    """Create tasks for each housekeeping tasks."""
    log("Initializing housekeeping tasks.", Ansi.LCYAN)

    loop = asyncio.get_running_loop()

    app.state.sessions.housekeeping_tasks.update(
        {
            loop.create_task(task)
            for task in (
                _remove_expired_donation_privileges(interval=30 * 60),
                _update_bot_status(interval=5 * 60),
                _disconnect_ghosts(interval=OSU_CLIENT_MIN_PING_INTERVAL // 3),
                _check_betmap_status(interval=60),
            )
        },
    )


async def _remove_expired_donation_privileges(interval: int) -> None:
    """Remove donation privileges from users with expired sessions."""
    while True:
        if app.settings.DEBUG:
            log("Removing expired donation privileges.", Ansi.LMAGENTA)

        expired_donors = await app.state.services.database.fetch_all(
            "SELECT id FROM users "
            "WHERE donor_end <= UNIX_TIMESTAMP() "
            "AND priv & :donor_priv",
            {"donor_priv": Privileges.DONATOR.value},
        )

        for expired_donor in expired_donors:
            player = await app.state.sessions.players.from_cache_or_sql(
                id=expired_donor["id"],
            )

            assert player is not None

            # TODO: perhaps make a `revoke_donor` method?
            await player.remove_privs(Privileges.DONATOR)
            await player.remove_privs(Privileges.VOTER)
            player.donor_end = 0
            await app.state.services.database.execute(
                "UPDATE users SET donor_end = 0 WHERE id = :id",
                {"id": player.id},
            )

            if player.is_online:
                player.enqueue(
                    app.packets.notification("Your supporter status has expired."),
                )

            log(f"{player}'s supporter status has expired.", Ansi.LMAGENTA)

        await asyncio.sleep(interval)



async def _disconnect_ghosts(interval: int) -> None:
    """Actively disconnect users above the
    disconnection time threshold on the osu! server."""
    while True:
        await asyncio.sleep(interval)
        current_time = time.time()

        for player in app.state.sessions.players:
            if current_time - player.last_recv_time > OSU_CLIENT_MIN_PING_INTERVAL:
                log(f"Auto-dced {player}.", Ansi.LMAGENTA)
                player.logout()


async def _update_bot_status(interval: int) -> None:
    """Re roll the bot status, every `interval`."""
    while True:
        await asyncio.sleep(interval)
        app.packets.bot_stats.cache_clear()


async def _check_betmap_status(interval: int) -> None:
    while True:
        print("Checking beatmap status.")
        await asyncio.sleep(interval)

        # Calculate the threshold time for ranked beatmaps but one day ago
        if app.settings.DEVELOPER_MODE:
            one_day_ago = datetime.now() - timedelta(minutes=1)
        else:
            one_day_ago = datetime.now() - timedelta(minutes=1440)
        # make to unix timestamp
        one_day_ago = one_day_ago.timestamp()
        # Use parameterized query for security and efficiency
        qualified_beatmaps = await app.state.services.database.fetch_all(
            "SELECT set_id FROM maps WHERE status = 4 AND change_date < :threshold_time",
            {"threshold_time": datetime.fromtimestamp(one_day_ago)}
        )
        alreadyrank = []

        for beatmap_id in qualified_beatmaps:
            # Extract id directly for efficiency
            id = beatmap_id["set_id"]
            await app.state.services.database.execute(
                "UPDATE maps SET status = 2 WHERE set_id = :id",
                {"id": id}
            )
            bmap_artist = await app.state.services.database.fetch_val(
                "SELECT artist FROM maps WHERE set_id = :id",
                {"id": id}
            )
            bmap_title = await app.state.services.database.fetch_val(
                "SELECT title FROM maps WHERE set_id = :id",
                {"id": id}
            )
            bmap_creator = await app.state.services.database.fetch_val(
                "SELECT creator FROM maps WHERE set_id = :id",
                {"id": id}
            )
            bmap_version = await app.state.services.database.fetch_val(
                "SELECT version FROM maps WHERE set_id = :id",
                {"id": id}
            )
            
            # Remove change_date from maps
            #await app.state.services.database.execute(
            #    "UPDATE maps SET change_date = NULL WHERE set_id = :id",
            #    {"id": beatmap_id}
            #)
            # Generate new webhook, with url to beatmap
            if id not in alreadyrank:
                alreadyrank.append(id)
                if webhook_url := app.settings.DISCORD_NOMINATION_WEBHOOK:
                    embed = Embed(title="", description=f"[{bmap_artist} - {bmap_title} ({bmap_creator})](https://osu.ppy.sh/beatmapsets/{id}) is now ranked!", timestamp=datetime.utcnow(), color=52478)
                    embed.set_author(name="Automatic Status Bot (Click to get beatmap!)", icon_url="https://a.ppy.sh/1", url=f"https://osu.ppy.sh/beatmapsets/{id}")
                    embed.set_image(url=f"https://assets.ppy.sh/beatmaps/{id}/covers/card.jpg")
                    embed.set_footer(text="Nomination Tools")
                    webhook = Webhook(webhook_url, embeds=[embed])
                    await asyncio.create_task(webhook.post())
            
            
            # Getting beatmap id by set_id
            map_ids = await app.state.services.database.fetch_all(
                "SELECT id FROM maps WHERE set_id = :id",
                {"id": id}
            )
            for map_id in map_ids:
                # make sure db is updated
                # like this
                # for _bmap in app.state.cache.beatmapset[bmap.set_id].maps:
                #                _bmap.status = new_status
                #                _bmap.frozen = True
                md5 = await app.state.services.database.fetch_val(
                    "SELECT md5 FROM maps WHERE id = :id",
                    {"id": map_id}
                )
                if md5 in app.state.cache.beatmap:
                    app.state.cache.beatmap[md5].status = 2
                    app.state.cache.beatmap[md5].frozen = True
                # delete request from map_requests (map_id)
                await app.state.services.database.execute(
                    "DELETE FROM map_requests WHERE map_id = :id",
                    {"id": map_id}
                )
                # Delete all scores for this beatmap
                await app.state.services.database.execute(
                    "DELETE FROM scores WHERE map_md5 = :map_md5",
                    {"map_md5": md5}
                )
                log(f"Beatmap {id} has been ranked.", Ansi.LMAGENTA)

