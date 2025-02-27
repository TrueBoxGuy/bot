import sqlalchemy
import sqlalchemy.schema
import sqlalchemy.orm
import sqlalchemy.ext.asyncio
import sqlalchemy.dialects.postgresql
import discord
import discord.ext.commands
from typing import Protocol, cast
import util.db
import util.db.kv
import util.frozen_list
import plugins
import plugins.cogs

registry: sqlalchemy.orm.registry = sqlalchemy.orm.registry()

engine = util.db.create_async_engine()
plugins.finalizer(engine.dispose)

@registry.mapped
class MemberRole:
    __tablename__ = "member_roles"
    __table_args__ = {"schema": "persistence"}

    user_id: int = sqlalchemy.Column(sqlalchemy.BigInteger, primary_key=True)
    role_id: int = sqlalchemy.Column(sqlalchemy.BigInteger, primary_key=True)

class PersistenceConf(Protocol):
    roles: util.frozen_list.FrozenList[int]

conf: PersistenceConf

@plugins.init
async def init() -> None:
    global conf
    conf = cast(PersistenceConf, await util.db.kv.load(__name__))
    await util.db.init(util.db.get_ddl(
        sqlalchemy.schema.CreateSchema("persistence").execute,
        registry.metadata.create_all))

@plugins.cogs.cog
class Persistence(discord.ext.commands.Cog):
    """Role persistence."""
    @discord.ext.commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        role_ids = set(role.id for role in member.roles if role.id in conf.roles)
        if len(role_ids) == 0: return
        async with sqlalchemy.ext.asyncio.AsyncSession(engine) as session:
            stmt = (sqlalchemy.dialects.postgresql.insert(MemberRole)
                .values([{"user_id": member.id, "role_id": role_id} for role_id in role_ids])
                .on_conflict_do_nothing(index_elements=["user_id", "role_id"]))
            await session.execute(stmt)
            await session.commit()

    @discord.ext.commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        async with sqlalchemy.ext.asyncio.AsyncSession(engine) as session:
            stmt = (sqlalchemy.delete(MemberRole)
                .where(MemberRole.user_id == member.id)
                .returning(MemberRole.role_id))
            roles = []
            for role_id, in await session.execute(stmt):
                if (role := member.guild.get_role(role_id)) is not None:
                    roles.append(role)
            if len(roles) == 0: return
            await member.add_roles(*roles, reason="Role persistence", atomic=False)
            await session.commit()

async def drop_persistent_role(*, user_id: int, role_id: int) -> None:
    async with sqlalchemy.ext.asyncio.AsyncSession(engine) as session:
        stmt = (sqlalchemy.delete(MemberRole)
            .where(MemberRole.user_id == user_id, MemberRole.role_id == role_id))
        await session.execute(stmt)
        await session.commit()
