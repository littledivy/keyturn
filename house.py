"""Data class representing a single apartment listing from Pararius."""

from dataclasses import dataclass


@dataclass
class House:
    """Stores details about a rental listing scraped from Pararius.

    Attributes:
        title: The listing title (e.g. "Apartment for rent").
        location: Neighborhood or street name.
        price: Monthly rent as displayed on the page.
        surface_area: Living area in square meters.
        link: Full URL to the listing on pararius.com.
    """

    title: str
    location: str
    price: str
    surface_area: str
    link: str
    image: str = ""
    rooms: int = 0
    interior: str = ""  # Shell / Upholstered / Furnished — filled from the detail page
    is_new: bool = False

    def to_list(self):
        """Return listing data as a list matching the DataFrame column order."""
        return [self.title, self.location, self.price, self.surface_area, self.link,
                self.image, self.rooms, self.interior, self.is_new, False]

    def __repr__(self):
        return f"House('{self.title}', '{self.location}', '{self.price}')"
