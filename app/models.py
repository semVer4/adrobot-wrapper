from sqlalchemy import JSON, Boolean, CheckConstraint, ForeignKey, Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class StreamRecord(Base):
    __tablename__ = "streams"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    campaign_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    missing: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    conflict: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    offers: Mapped[list["StreamOffer"]] = relationship(
        back_populates="stream", cascade="all, delete-orphan",
        order_by="StreamOffer.offer_id", lazy="selectin",
    )


class StreamOffer(Base):
    __tablename__ = "stream_offers"
    __table_args__ = (
        CheckConstraint("share BETWEEN 0 AND 100", name="ck_stream_offers_share"),
        CheckConstraint(
            "published_share BETWEEN 0 AND 100", name="ck_stream_offers_published_share"
        ),
    )

    stream_id: Mapped[int] = mapped_column(ForeignKey("streams.id"), primary_key=True)
    offer_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    share: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pinned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    archived: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    removal_snapshot: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    # A newly added draft offer has no published baseline. An archived offer does.
    published: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    published_share: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    published_pinned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    published_archived: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    stream: Mapped[StreamRecord] = relationship(back_populates="offers")
