import io
import csv
import collections
import discord
from typing import List, Dict, Set, Tuple, Callable, Awaitable, Union, Optional, Any
import util.discord
import plugins.commands
import plugins.privileges
import plugins.reactions

def channel_sort_key(channel: discord.abc.GuildChannel) -> Tuple[int, bool, int]:
    if isinstance(channel, discord.CategoryChannel):
        return (channel.position, False, -1)
    else:
        return (channel.category.position if channel.category is not None else -1,
            isinstance(channel, (discord.VoiceChannel, discord.StageChannel)),
            channel.position)

def overwrite_sort_key(pair: Tuple[Union[discord.Role, discord.Member], discord.PermissionOverwrite]) -> int:
    if isinstance(pair[0], discord.Role):
        try:
            return pair[0].guild.roles.index(pair[0])
        except ValueError:
            return -1
    else:
        return -1

def disambiguated_name(channel: discord.abc.GuildChannel) -> str:
    chans: List[discord.abc.GuildChannel] = [chan for chan in channel.guild.channels if chan.name == channel.name]
    if len(chans) < 2:
        return channel.name
    chans.sort(key=lambda chan: chan.id)
    return "{} ({})".format(channel.name, 1 + chans.index(channel))

@plugins.privileges.priv("mod")
@plugins.commands.command("exportperms")
async def exportperms(ctx: discord.ext.commands.Context) -> None:
    """Export all role and channel permission settings into CSV."""
    if ctx.guild is None:
        raise util.discord.InvocationError("This can only be used in a guild.")

    file = io.StringIO()
    writer = csv.writer(file)
    writer.writerow(["Category", "Channel", "Role/User"] + [flag for flag, _ in discord.Permissions()])

    for role in ctx.guild.roles:
        writer.writerow(["", "", "Role " + role.name] + ["+" if value else "-" for _, value in role.permissions])

    for channel in sorted(ctx.guild.channels, key=channel_sort_key):
        if isinstance(channel, discord.CategoryChannel):
            header = [disambiguated_name(channel), ""]
        else:
            header = [disambiguated_name(channel.category) if channel.category is not None else "",
                disambiguated_name(channel)]
        writer.writerow(header + ["(synced)" if channel.permissions_synced else ""])
        if channel.permissions_synced: continue
        for target, overwrite in sorted(channel.overwrites.items(), key=overwrite_sort_key):
            if isinstance(target, discord.Role):
                name = "Role {}".format(target.name)
            else:
                name = "User {} {}".format(target.id, target.name)
            writer.writerow(header + [name] + ["+" if allow else "-" if deny else "/"
                for (flag, allow), (_, deny) in zip(*overwrite.pair())])

    await ctx.send(file=discord.File(io.BytesIO(file.getvalue().encode()), "perms.csv"))

def tweak_permissions(permissions: discord.Permissions, add_mask: int, remove_mask: int) -> discord.Permissions:
    return discord.Permissions(permissions.value & ~remove_mask | add_mask)

def tweak_overwrite(overwrite: discord.PermissionOverwrite,
    add_mask: int, remove_mask: int, reset_mask: int) -> discord.PermissionOverwrite:
    allow, deny = overwrite.pair()
    return discord.PermissionOverwrite.from_pair(
        tweak_permissions(allow, add_mask, reset_mask),
        tweak_permissions(deny, remove_mask, reset_mask))

def overwrites_for(channel: discord.abc.GuildChannel, target: Union[discord.Role, discord.Member]
    ) -> discord.PermissionOverwrite:
    for t, overwrite in channel.overwrites.items():
        if t.id == target.id:
            return overwrite
    return discord.PermissionOverwrite()

@plugins.privileges.priv("admin")
@plugins.commands.command("importperms")
async def importperms(ctx: discord.ext.commands.Context) -> None:
    """Import all role and channel permission settings from an attached CSV file."""
    if ctx.guild is None:
        raise util.discord.InvocationError("This can only be used in a guild.")
    if len(ctx.message.attachments) != 1:
        raise util.discord.InvocationError("Expected 1 attachment.")
    file = io.StringIO((await ctx.message.attachments[0].read()).decode())
    reader = csv.reader(file)

    channels = {disambiguated_name(channel): channel for channel in ctx.guild.channels}
    roles = {role.name: role for role in ctx.guild.roles}

    header = next(reader)
    if len(header) < 3 or header[0] != "Category" or header[1] != "Channel" or header[2] != "Role/User":
        raise util.discord.UserError("Invalid header.")

    flags: List[Tuple[Any, str]] = []
    for perm in header[3:]:
        try:
            flags.append((getattr(discord.Permissions, perm), perm))
        except AttributeError:
            raise util.discord.UserError("Unknown permission: {!r}".format(perm))

    actions: List[Callable[[], Awaitable[Any]]] = []
    output: List[str] = []
    new_overwrites: Dict[discord.abc.GuildChannel, Dict[Union[discord.Role, discord.Member], Tuple[int, int, int]]]
    new_overwrites = collections.defaultdict(dict)
    overwrites_changed: Set[discord.abc.GuildChannel] = set()
    want_sync: Set[discord.abc.GuildChannel] = set()
    seen_moved: Dict[discord.abc.GuildChannel, Optional[discord.CategoryChannel]] = {}

    for row in reader:
        if len(row) < 3:
            raise util.discord.UserError("Line {}: invalid row.".format(reader.line_num))
        if row[0] == "" and row[1] == "":
            if not row[2].startswith("Role "):
                raise util.discord.UserError("Line {}: expected a role.".format(reader.line_num))
            role_name = row[2].removeprefix("Role ")
            if role_name not in roles:
                raise util.discord.UserError("Line {}: unknown role {!r}.".format(reader.line_num, role_name))
            role = roles[role_name]
            changes = []
            add_mask = 0
            remove_mask = 0
            for (flag, perm), sign in zip(flags, row[3:]):
                if sign == "+" and not role.permissions.value & flag.flag:
                    changes.append("\u2705" + perm)
                    add_mask |= flag.flag
                if sign == "-" and role.permissions.value & flag.flag:
                    changes.append("\u274C" + perm)
                    remove_mask |= flag.flag
            if changes:
                output.append(util.discord.format("{!M}: {}", role, ", ".join(changes)))
            if add_mask != 0 or remove_mask != 0:
                actions.append((lambda role, add_mask, remove_mask: lambda:
                    role.edit(permissions=discord.Permissions(role.permissions.value & ~remove_mask | add_mask)))
                    (role, add_mask, remove_mask))
        else:
            category: Optional[discord.CategoryChannel]
            channel: Union[discord.CategoryChannel,
                discord.TextChannel, discord.StoreChannel, discord.VoiceChannel, discord.StageChannel]
            if row[1] == "":
                category = None
                if row[0] not in channels:
                    raise util.discord.UserError("Line {}: unknown channel {!r}.".format(reader.line_num, row[0]))
                channel = channels[row[0]]
                if not isinstance(channel, discord.CategoryChannel):
                    raise util.discord.UserError("Line {}: {!r} is not a category.".format(reader.line_num, row[0]))
            else:
                if row[0] == "":
                    category = None
                else:
                    if row[0] not in channels:
                        raise util.discord.UserError("Line {}: unknown channel {!r}.".format(reader.line_num, row[0]))
                    cat = channels[row[0]]
                    if not isinstance(cat, discord.CategoryChannel):
                        raise util.discord.UserError("Line {}: {!r} is not a category.".format(reader.line_num, row[0]))
                    category = cat
                if row[1] not in channels:
                    raise util.discord.UserError("Line {}: unknown channel {!r}.".format(reader.line_num, row[1]))
                channel = channels[row[1]]
                if isinstance(channel, discord.CategoryChannel):
                    raise util.discord.UserError("Line {}: {!r} is a category.".format(reader.line_num, row[1]))

            if not isinstance(channel, discord.CategoryChannel) and channel.category != category:
                if not channel in seen_moved:
                    seen_moved[channel] = category
                    output.append(util.discord.format("Move {!c} to {!c}", channel, category))
                    actions.append((lambda channel, category: lambda:
                        channel.edit(category=category))
                        (channel, category))

            if row[2] == "(synced)" and not isinstance(channel, discord.CategoryChannel):
                want_sync.add(channel)
            elif row[2] != "":
                target: Union[discord.Role, discord.Member]
                if row[2].startswith("Role "):
                    role_name = row[2].removeprefix("Role ")
                    if role_name not in roles:
                        raise util.discord.UserError("Line {}: unknown role {!r}.".format(reader.line_num, role_name))
                    target = roles[role_name]
                elif row[2].startswith("User "):
                    try:
                        user_id = int(row[2].removeprefix("User ").split(maxsplit=1)[0])
                    except ValueError:
                        raise util.discord.UserError("Line {}: expected user ID".format(reader.line_num))
                    if (member := ctx.guild.get_member(user_id)) is None:
                        raise util.discord.UserError("Line {}: no such member {}.".format(reader.line_num, user_id))
                    target = member
                else:
                    raise util.discord.UserError("Line {}: expected a role or user.".format(reader.line_num))
                allow, deny = overwrites_for(channel, target).pair()
                changes = []
                add_mask = 0
                remove_mask = 0
                reset_mask = 0
                for (flag, perm), sign in zip(flags, row[3:]):
                    if sign == "+" and not allow.value & flag.flag:
                        changes.append("\u2705" + perm)
                        add_mask |= flag.flag
                    if sign == "-" and not deny.value & flag.flag:
                        changes.append("\u274C" + perm)
                        remove_mask |= flag.flag
                    if sign == "/" and (allow.value & flag.flag or deny.value & flag.flag):
                        changes.append("\U0001F533" + perm)
                        reset_mask |= flag.flag
                if changes:
                    output.append(util.discord.format(
                        "{!c} {!M}: {}" if isinstance(target, discord.Role) else "{!c} {!m}: {}",
                        channel, target, ", ".join(changes)))
                new_overwrites[channel][target] = (add_mask, remove_mask, reset_mask)
            else:
                new_overwrites[channel]
    for chan in new_overwrites:
        if channel in want_sync: continue
        if not isinstance(chan, (discord.CategoryChannel,
            discord.TextChannel, discord.StoreChannel, discord.VoiceChannel, discord.StageChannel)): continue
        channel = chan
        for add_mask, remove_mask, reset_mask in new_overwrites[channel].values():
            if add_mask != 0 or remove_mask != 0 or reset_mask != 0:
                overwrites_changed.add(channel)
                break
        for target in channel.overwrites:
            if target not in new_overwrites[channel]:
                output.append(util.discord.format(
                    "{!c} remove {!M}" if isinstance(target, discord.Role) else "{!c} remove {!m}",
                    channel, target))
                overwrites_changed.add(channel)
        actions.append((lambda channel: lambda:
            channel.edit(overwrites={
                target: tweak_overwrite(overwrites_for(channel, target), add_mask, remove_mask, reset_mask)
                for target, (add_mask, remove_mask, reset_mask) in new_overwrites[channel].items()}))
            (channel))
    for chan in want_sync:
        if not isinstance(chan,
            (discord.TextChannel, discord.StoreChannel, discord.VoiceChannel, discord.StageChannel)): continue
        channel = chan
        new_category = seen_moved.get(channel, channel.category)
        if new_category is None:
            raise util.discord.UserError(util.discord.format("Cannot sync channel {!c} with no category", channel))
        if not channel.permissions_synced or channel in seen_moved or new_category in overwrites_changed:
            output.append(util.discord.format("Sync {!c} with {!c}", channel, new_category))
            actions.append((lambda channel: lambda:
                channel.edit(sync_permissions=True))
                (channel))

    if not output:
        await ctx.send("No changes.")
        return

    text = ""
    for out in output:
        if len(text) + 1 + len(out) > 2000:
            await ctx.send(text, allowed_mentions=discord.AllowedMentions.none())
            text = out
        else:
            text += "\n" + out
    msg = await ctx.send(text, allowed_mentions=discord.AllowedMentions.none())

    if await plugins.reactions.get_reaction(msg, ctx.author, {"\u274C": False, "\u2705": True}, timeout=300):
        for action in actions:
            await action()
        await ctx.send("\u2705")
