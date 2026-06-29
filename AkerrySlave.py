import os
import asyncio
import discord
import yt_dlp
import re
import spotipy
import json
from spotipy.oauth2 import SpotifyClientCredentials
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')

# Spotify API setup (optional - only required for Spotify links)
SPOTIFY_CLIENT_ID = os.getenv('SPOTIFY_CLIENT_ID')
SPOTIFY_CLIENT_SECRET = os.getenv('SPOTIFY_CLIENT_SECRET')
spotify_client = None

if SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET:
    try:
        auth_manager = SpotifyClientCredentials(client_id=SPOTIFY_CLIENT_ID, client_secret=SPOTIFY_CLIENT_SECRET)
        spotify_client = spotipy.Spotify(auth_manager=auth_manager)
    except Exception as e:
        print(f"Warning: Could not initialize Spotify API: {e}")
        print("Spotify link support will be limited. Set SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET in .env to enable it.")
else:
    print("Note: SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET not set in .env. Spotify link support disabled.")
    print("To enable Spotify links, add credentials to your .env file.")


# FFmpeg: prefer FFMPEG_PATH env var, else try bundled ./bin/ffmpeg/ffmpeg.exe, else assume `ffmpeg` is on PATH
_here = os.path.dirname(__file__)
_bundled_ffmpeg = os.path.join(_here, 'bin', 'ffmpeg', 'ffmpeg.exe')
FFMPEG = os.getenv('FFMPEG_PATH') or (_bundled_ffmpeg if os.path.exists(_bundled_ffmpeg) else 'ffmpeg')

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True

bot = commands.Bot(command_prefix='!', intents=intents)

# Persistent data storage for user book progress
DATA_FILE = os.path.join(os.path.dirname(__file__), 'user_data.json')

def load_user_books() -> dict:
    """Load user book progress from JSON file."""
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, 'r') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            print(f"Warning: Could not load user data: {e}")
            return {}
    return {}

def save_user_books(data: dict) -> None:
    """Save user book progress to JSON file."""
    try:
        with open(DATA_FILE, 'w') as f:
            json.dump(data, f, indent=2)
    except IOError as e:
        print(f"Error: Could not save user data: {e}")


def parse_spotify_url(url: str) -> dict | None:
    """
    Extract track info from a Spotify URL and fetch metadata from Spotify API.
    Supports formats like:
    - https://open.spotify.com/track/TRACK_ID
    - https://open.spotify.com/track/TRACK_ID?si=...
    """
    # Match Spotify track URL
    match = re.search(r'spotify\.com/track/([a-zA-Z0-9]+)', url)
    if not match:
        return None
    
    track_id = match.group(1)
    
    # If Spotify API is available, fetch track metadata
    if spotify_client:
        try:
            track = spotify_client.track(track_id)
            artists = ', '.join([artist['name'] for artist in track['artists']])
            title = track['name']
            search_query = f"{artists} - {title}"
            return {
                'type': 'spotify',
                'track_id': track_id,
                'url': url,
                'search_query': search_query,
                'title': title,
                'artist': artists,
            }
        except Exception as e:
            print(f"Warning: Could not fetch Spotify track {track_id}: {e}")
            # Fallback: use track ID as search term
            return {
                'type': 'spotify',
                'track_id': track_id,
                'url': url,
                'search_query': track_id,
            }
    else:
        # Spotify API not available; use track ID as fallback search
        return {
            'type': 'spotify',
            'track_id': track_id,
            'url': url,
            'search_query': track_id,
        }


class YTDLSource:
    # Prefer higher bitrate audio formats (320kbps or closest available)
    ytdl_format_options = {
        'format': 'bestaudio[abr>128]/bestaudio/best',
        'noplaylist': True,
        'quiet': True,
        'skip_download': True,
        'default_search': 'ytsearch',
        'socket_timeout': 30,
    }

    ytdl = yt_dlp.YoutubeDL(ytdl_format_options)

    @classmethod
    async def create_source(cls, search: str, loop: asyncio.AbstractEventLoop = None):
        loop = loop or asyncio.get_event_loop()

        def extract():
            return cls.ytdl.extract_info(search, download=False)

        data = await loop.run_in_executor(None, extract)
        if not data:
            raise Exception('Could not retrieve any data from yt-dlp')

        # when searching, yt-dlp returns 'entries'
        if 'entries' in data:
            data = data['entries'][0]

        # Extract bitrate if available
        bitrate = None
        if 'format' in data and data['format']:
            format_info = data['format']
            if 'abr' in format_info and format_info['abr']:
                bitrate = int(format_info['abr'])
            elif 'tbr' in format_info and format_info['tbr']:
                bitrate = int(format_info['tbr'])

        return {
            'source': data.get('url'),
            'title': data.get('title'),
            'webpage_url': data.get('webpage_url'),
            'bitrate': bitrate,
            'duration': data.get('duration'),
        }


class GuildMusic:
    """Per-guild music player with an asyncio queue."""

    def __init__(self, bot: commands.Bot, guild: discord.Guild):
        self.bot = bot
        self.guild = guild
        self.queue = asyncio.Queue()
        self.next = asyncio.Event()
        self.player = None
        self.current = None
        self.loop_enabled = False
        self.now_playing_msg = None  # Discord message object for the now-playing embed
        self.now_playing_channel = None  # Channel to post the now-playing embed
        self.track_start_time = None  # When current track started playing
        self._task = bot.loop.create_task(self.player_loop())
        self._progress_task = bot.loop.create_task(self.progress_update_loop())

    async def player_loop(self):
        while True:
            try:
                self.next.clear()
                item = await self.queue.get()
                self.current = item
                self.track_start_time = asyncio.get_event_loop().time()
                source = discord.FFmpegPCMAudio(item['source'], executable=FFMPEG,
                                                 before_options='-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5')
                vc = self.guild.voice_client
                if not vc or not vc.is_connected():
                    # if player is not connected, skip until connected
                    await asyncio.sleep(0.5)
                    continue

                await self.send_now_playing()
                vc.play(source, after=lambda e: self.bot.loop.call_soon_threadsafe(self.next.set))
                await self.next.wait()
                # clean up
                try:
                    source.cleanup()
                except Exception:
                    pass
                self.current = None
                self.track_start_time = None
                # re-enqueue if loop is enabled
                if self.loop_enabled and item:
                    self.queue.put_nowait(item)
            except Exception as e:
                print(f"Error in player_loop: {e}")
                await asyncio.sleep(1)
                continue

    def add_to_queue(self, item: dict):
        self.queue.put_nowait(item)

    def build_progress_bar(self, elapsed: int, duration: int, bar_length: int = 15) -> str:
        """Build a visual progress bar."""
        if not duration or duration == 0:
            return '▓' * bar_length + ' 0:00 / 0:00'
        
        percentage = min(elapsed / duration, 1.0)
        filled = int(bar_length * percentage)
        bar = '█' * filled + '░' * (bar_length - filled)
        
        elapsed_mins, elapsed_secs = divmod(elapsed, 60)
        dur_mins, dur_secs = divmod(duration, 60)
        
        return f'{bar} {int(elapsed_mins)}:{int(elapsed_secs):02d} / {int(dur_mins)}:{int(dur_secs):02d}'

    async def progress_update_loop(self):
        """Periodically update the now-playing embed with progress."""
        while True:
            try:
                await asyncio.sleep(10)  # Update every 10 seconds (was 5) to reduce API calls
                if self.current and self.track_start_time and self.now_playing_msg:
                    # Only update if duration is available
                    duration = self.current.get('duration')
                    if duration:
                        try:
                            await self.send_now_playing()
                        except Exception as e:
                            # Silently ignore progress update errors to avoid spam
                            pass
            except Exception as e:
                print(f"Error in progress_update_loop: {e}")
                await asyncio.sleep(1)

    async def send_now_playing(self):
        """Send or update the now-playing embed message."""
        if not self.current or not self.now_playing_channel:
            return

        embed = discord.Embed(
            title='🎵 Now Playing',
            description=f"**{self.current.get('title', 'Unknown')}**",
            color=discord.Color.blue()
        )
        embed.add_field(name='Requested by', value=self.current.get('requester', 'Unknown'), inline=False)
        
        # Build audio info line with bitrate and duration
        audio_info = []
        
        bitrate = self.current.get('bitrate')
        if bitrate:
            quality = '🟢 Excellent' if bitrate >= 256 else '� High' if bitrate >= 192 else '🟡 Good' if bitrate >= 128 else '🔴 Fair'
            audio_info.append(f'{quality} ({bitrate} kbps)')
        else:
            audio_info.append('🔊 Stream')
        
        duration = self.current.get('duration')
        if duration:
            minutes, seconds = divmod(duration, 60)
            audio_info.append(f'⏱️ {int(minutes)}:{int(seconds):02d}')
        
        if audio_info:
            embed.add_field(name='Audio Quality', value=' • '.join(audio_info), inline=False)
        
        # Add progress bar
        if duration and self.track_start_time:
            elapsed = int(asyncio.get_event_loop().time() - self.track_start_time)
            progress_bar = self.build_progress_bar(elapsed, duration)
            embed.add_field(name='Progress', value=f'`{progress_bar}`', inline=False)
        
        embed.add_field(name='Loop', value='✅ Enabled' if self.loop_enabled else '❌ Disabled', inline=False)

        try:
            if self.now_playing_msg is None:
                # Send new message
                self.now_playing_msg = await self.now_playing_channel.send(embed=embed)
                # Add reaction emotes concurrently for faster loading
                emotes = ['▶️', '⏸️', '⏹️', '⏭️', '🔁']
                await asyncio.gather(*[self.now_playing_msg.add_reaction(emote) for emote in emotes], return_exceptions=True)
            else:
                # Update existing message (no need to re-add reactions)
                await self.now_playing_msg.edit(embed=embed)
        except Exception as e:
            print(f"Error updating now-playing message: {e}")


def get_book_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    """Autocomplete function to suggest existing book titles."""
    user_data = load_user_books()
    all_books = set()
    
    # Collect all unique book titles across all users
    for user_id, books in user_data.items():
        for book_name in books.keys():
            all_books.add(book_name)
    
    # Filter and sort books matching current input
    matches = [book for book in all_books if current.lower() in book.lower()]
    matches.sort()
    
    # Return up to 25 choices (Discord limit)
    return [app_commands.Choice(name=book, value=book) for book in matches[:25]]


players: dict[int, GuildMusic] = {}


async def get_book_autocomplete_async(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    """Async autocomplete function to suggest existing book titles."""
    user_data = load_user_books()
    all_books = set()
    
    # Collect all unique book titles across all users
    for user_id, books in user_data.items():
        for book_name in books.keys():
            all_books.add(book_name)
    
    # Filter and sort books matching current input
    matches = [book for book in all_books if current.lower() in book.lower()]
    matches.sort()
    
    # Return up to 25 choices (Discord limit)
    return [app_commands.Choice(name=book, value=book) for book in matches[:25]]


def get_player(guild: discord.Guild) -> GuildMusic:
    if guild.id not in players:
        players[guild.id] = GuildMusic(bot, guild)
    return players[guild.id]


@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f"{bot.user} is now online!")


@bot.event
async def on_voice_state_update(member, before, after):
    """Log when bot disconnects from voice."""
    if member == bot.user:
        if before.channel and not after.channel:
            print(f"Bot disconnected from {before.channel.name}")
        elif not before.channel and after.channel:
            print(f"Bot connected to {after.channel.name}")


@bot.tree.command(name='join', description='Make the bot join your voice channel')
async def join(interaction: discord.Interaction):
    try:
        if not interaction.user.voice or not interaction.user.voice.channel:
            await interaction.response.send_message('❌ You are not connected to a voice channel.', ephemeral=True)
            return

        await interaction.response.defer()
        
        channel = interaction.user.voice.channel
        vc = interaction.guild.voice_client
        
        try:
            if vc and vc.is_connected():
                await vc.move_to(channel)
            else:
                await channel.connect()
            await interaction.followup.send(f'✅ Joined {channel.name}')
        except discord.ClientException as e:
            await interaction.followup.send(f'❌ Failed to join channel: {e}', ephemeral=True)
        except asyncio.TimeoutError:
            await interaction.followup.send('❌ Connection timed out. Please try again.', ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f'❌ An unexpected error occurred: {str(e)[:100]}', ephemeral=True)
        print(f"Error in /join: {e}")


@bot.tree.command(name='leave', description='Disconnect the bot from voice channel')
async def leave(interaction: discord.Interaction):
    try:
        vc = interaction.guild.voice_client
        if not vc or not vc.is_connected():
            await interaction.response.send_message('❌ Bot is not connected to a voice channel.', ephemeral=True)
            return
        
        try:
            await vc.disconnect()
            await interaction.response.send_message('✅ Disconnected.')
        except Exception as e:
            await interaction.response.send_message(f'❌ Failed to disconnect: {str(e)[:100]}', ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f'❌ An unexpected error occurred: {str(e)[:100]}', ephemeral=True)
        print(f"Error in /leave: {e}")


@bot.tree.command(name='play', description='Play a URL or search term on YouTube, or a Spotify link')
@app_commands.describe(query='A URL (YouTube or Spotify), or a search term')
async def play(interaction: discord.Interaction, query: str):
    await interaction.response.defer()
    
    try:
        # Ensure user is in voice channel
        if not interaction.user.voice or not interaction.user.voice.channel:
            await interaction.followup.send('❌ You need to be in a voice channel to play audio.', ephemeral=True)
            return

        channel = interaction.user.voice.channel
        vc = interaction.guild.voice_client
        if not vc or not vc.is_connected():
            try:
                await channel.connect()
            except discord.ClientException as e:
                await interaction.followup.send(f'❌ Failed to connect to voice channel: {str(e)[:100]}', ephemeral=True)
                return

        # Check if it's a Spotify link and convert to search query
        spotify_info = parse_spotify_url(query)
        if spotify_info:
            # Use track ID as search term; yt-dlp will find similar tracks on YouTube
            query = spotify_info["track_id"]
            await interaction.followup.send('🎵 Searching for Spotify track on YouTube...', ephemeral=True)

        try:
            info = await YTDLSource.create_source(query)
        except Exception as e:
            error_msg = str(e)
            if 'ERROR' in error_msg or 'not found' in error_msg.lower():
                await interaction.followup.send('❌ Track not found. Try a different search or URL.', ephemeral=True)
            else:
                await interaction.followup.send(f'❌ Error extracting audio: {error_msg[:100]}', ephemeral=True)
            return

        item = {
            'source': info['source'],
            'title': info.get('title', 'Unknown Track'),
            'requester': interaction.user.mention,
            'bitrate': info.get('bitrate'),
            'duration': info.get('duration'),
        }
        player = get_player(interaction.guild)
        player.now_playing_channel = interaction.channel
        player.add_to_queue(item)

        await interaction.followup.send(f"✅ Queued: {item['title']}")
    except Exception as e:
        await interaction.followup.send(f'❌ An unexpected error occurred: {str(e)[:100]}', ephemeral=True)
        print(f"Error in /play: {e}")


@bot.tree.command(name='stop', description='Stop playback and clear the queue')
async def stop(interaction: discord.Interaction):
    try:
        vc = interaction.guild.voice_client
        if not vc or not vc.is_connected():
            await interaction.response.send_message('❌ Bot is not connected to voice.', ephemeral=True)
            return
        if not (vc.is_playing() or vc.is_paused()):
            await interaction.response.send_message('❌ Nothing is playing.', ephemeral=True)
            return
        
        vc.stop()
        # purge queue
        player = get_player(interaction.guild)
        while not player.queue.empty():
            try:
                player.queue.get_nowait()
                player.queue.task_done()
            except asyncio.QueueEmpty:
                break
        await interaction.response.send_message('⏹️ Stopped and cleared queue.')
    except Exception as e:
        await interaction.response.send_message(f'❌ Error stopping playback: {str(e)[:100]}', ephemeral=True)
        print(f"Error in /stop: {e}")


@bot.tree.command(name='queue', description='Show the next items in queue')
async def show_queue(interaction: discord.Interaction):
    try:
        player = get_player(interaction.guild)
        
        # non-destructive peek: copy queue items if possible
        items = []
        q = player.queue._queue if hasattr(player.queue, '_queue') else []
        for entry in list(q)[:10]:
            items.append(entry.get('title', 'Unknown'))
        
        # Create embed
        embed = discord.Embed(
            title='🎵 Music Queue',
            color=discord.Color.purple(),
            description=f'Showing up to 10 queued songs'
        )
        
        if not items:
            embed.add_field(name='Queue', value='The queue is empty.', inline=False)
            await interaction.response.send_message(embed=embed)
            return
        
        # Add currently playing track if available
        if player.current:
            embed.add_field(
                name='🎧 Now Playing',
                value=f"**{player.current.get('title', 'Unknown')}**\nRequested by: {player.current.get('requester', 'Unknown')}",
                inline=False
            )
        
        # Add queue items with numbering
        queue_text = '\n'.join([f'{i+1}. {title}' for i, title in enumerate(items)])
        embed.add_field(
            name=f'Next ({len(items)} song{"s" if len(items) != 1 else ""})',
            value=queue_text,
            inline=False
        )
        
        await interaction.response.send_message(embed=embed)
    except Exception as e:
        await interaction.response.send_message(f'❌ Error displaying queue: {str(e)[:100]}', ephemeral=True)
        print(f"Error in /queue: {e}")


@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    """Handle reaction emotes on the now-playing message."""
    # Fast exit: ignore bot's own reactions
    if payload.user_id == bot.user.id:
        return

    # Get guild and player with early exit
    guild = bot.get_guild(payload.guild_id)
    if not guild:
        return

    player = get_player(guild)
    # Early exit if message doesn't match or player has no message
    if not player.now_playing_msg or payload.message_id != player.now_playing_msg.id:
        return

    # Get voice client once
    vc = guild.voice_client
    if not vc or not vc.is_connected():
        return

    emoji = str(payload.emoji)
    
    # Use dictionary for O(1) emoji handling instead of sequential if/elif
    emoji_handlers = {
        '▶️': lambda: vc.is_paused() and vc.resume(),
        '⏸️': lambda: vc.is_playing() and vc.pause(),
        '⏭️': lambda: (vc.is_playing() or vc.is_paused()) and vc.stop(),
    }
    
    # Handle simple reactions
    if emoji in emoji_handlers:
        emoji_handlers[emoji]()
        return
    
    # Handle stop (requires queue clearing)
    if emoji == '⏹️':
        if vc.is_playing() or vc.is_paused():
            vc.stop()
            # Clear queue efficiently
            while not player.queue.empty():
                try:
                    player.queue.get_nowait()
                    player.queue.task_done()
                except asyncio.QueueEmpty:
                    break
        return
    
    # Handle loop toggle
    if emoji == '🔁':
        player.loop_enabled = not player.loop_enabled
        # Update embed asynchronously without awaiting
        asyncio.create_task(player.send_now_playing())


# Book Progress Commands
@bot.tree.command(name='bookprogress', description='Track your book reading progress')
@app_commands.describe(
    action='Action to perform: set, view, or clear',
    book='Book name (required for set/clear)',
    chapter='Chapter number (required for set)',
    image_url='URL to book cover image (optional for set)'
)
@app_commands.autocomplete(book=get_book_autocomplete_async)
async def book_progress(
    interaction: discord.Interaction,
    action: str,
    book: str = None,
    chapter: str = None,
    image_url: str = None
):
    """Manage book reading progress with optional cover images."""
    try:
        user_id = str(interaction.user.id)
        user_data = load_user_books()
        
        # Initialize user dict if doesn't exist
        if user_id not in user_data:
            user_data[user_id] = {}
        
        # SET action: store/update book chapter and optional image
        if action.lower() == 'set':
            if not book or not chapter:
                await interaction.response.send_message(
                    '❌ Set requires both book name and chapter number\nUsage: `/bookprogress set "Book Name" 5` or with image: `/bookprogress set "Book Name" 5 https://example.com/cover.jpg`',
                    ephemeral=True
                )
                return
            
            # Store data with optional image URL (normalize book name to lowercase)
            # This ensures "Harry Potter", "HARRY POTTER", "harry potter" are all treated as the same book
            book_normalized = book.lower()
            book_data = {'chapter': chapter}
            if image_url:
                book_data['image_url'] = image_url
            
            user_data[user_id][book_normalized] = book_data
            save_user_books(user_data)
            
            embed = discord.Embed(
                title='✅ Progress Updated',
                description=f'**{book_normalized}** → Chapter {chapter}',
                color=discord.Color.green()
            )
            if image_url:
                embed.set_thumbnail(url=image_url)
            
            await interaction.response.send_message(embed=embed)
        
        # VIEW action: show book progress
        elif action.lower() == 'view':
            if book:
                # Show specific book (normalize to lowercase)
                book_normalized = book.lower()
                if book_normalized not in user_data[user_id]:
                    await interaction.response.send_message(
                        f'❌ No progress recorded for "{book}"',
                        ephemeral=True
                    )
                    return
                
                book_entry = user_data[user_id][book_normalized]
                # Handle both old format (string) and new format (dict)
                if isinstance(book_entry, dict):
                    chapter = book_entry.get('chapter', 'Unknown')
                    image_url_stored = book_entry.get('image_url')
                else:
                    chapter = book_entry
                    image_url_stored = None
                
                embed = discord.Embed(
                    title=f'📖 {book}',
                    description=f'Chapter: **{chapter}**',
                    color=discord.Color.blue()
                )
                if image_url_stored:
                    embed.set_thumbnail(url=image_url_stored)
                
                await interaction.response.send_message(embed=embed)
            else:
                # Show all books
                books = user_data[user_id]
                if not books:
                    await interaction.response.send_message(
                        '📚 No books being tracked yet. Use `/bookprogress set` to add one!',
                        ephemeral=True
                    )
                    return
                
                embed = discord.Embed(
                    title='📚 Your Books',
                    color=discord.Color.blue()
                )
                for book_name, book_entry in sorted(books.items()):
                    # Handle both old format (string) and new format (dict)
                    if isinstance(book_entry, dict):
                        chapter = book_entry.get('chapter', 'Unknown')
                    else:
                        chapter = book_entry
                    embed.add_field(name=book_name, value=f'Chapter {chapter}', inline=False)
                
                await interaction.response.send_message(embed=embed)
        
        # CLEAR action: remove book progress
        elif action.lower() == 'clear':
            if not book:
                await interaction.response.send_message(
                    '❌ Clear requires a book name\nUsage: `/bookprogress clear "Book Name"`',
                    ephemeral=True
                )
                return
            
            book_normalized = book.lower()
            if book_normalized not in user_data[user_id]:
                await interaction.response.send_message(
                    f'❌ No progress recorded for "{book}"',
                    ephemeral=True
                )
                return
            
            del user_data[user_id][book_normalized]
            save_user_books(user_data)
            
            embed = discord.Embed(
                title='✅ Progress Cleared',
                description=f'Removed "{book}" from your tracking',
                color=discord.Color.green()
            )
            await interaction.response.send_message(embed=embed)
        
        else:
            await interaction.response.send_message(
                '❌ Invalid action. Use: set, view, or clear',
                ephemeral=True
            )
    
    except Exception as e:
        await interaction.response.send_message(
            f'❌ Error: {str(e)[:100]}',
            ephemeral=True
        )
        print(f"Error in /bookprogress: {e}")


# Book Update UI Command (Interactive buttons for chapter adjustment)
class BookUpdateView(discord.ui.View):
    """Interactive buttons for updating book progress."""
    def __init__(self, user_id: str, book_name: str, book_key: str, current_chapter: int):
        super().__init__(timeout=300)  # 5 minute timeout
        self.user_id = user_id
        self.book_name = book_name
        self.book_key = book_key  # Actual key stored in JSON
        self.current_chapter = current_chapter
        self.message = None
    
    async def update_chapter(self, new_chapter: int, interaction: discord.Interaction):
        """Update chapter and refresh the display."""
        if new_chapter < 0:
            await interaction.response.defer()
            return
        
        user_data = load_user_books()
        book_entry = user_data[self.user_id][self.book_key]
        book_entry['chapter'] = str(new_chapter)
        save_user_books(user_data)
        
        self.current_chapter = new_chapter
        embed = self.create_embed()
        await interaction.response.edit_message(embed=embed, view=self)
    
    def create_embed(self) -> discord.Embed:
        """Create the book update embed."""
        embed = discord.Embed(
            title=f'📖 {self.book_name}',
            description=f'Chapter: **{self.current_chapter}**',
            color=discord.Color.blue()
        )
        embed.set_footer(text='Use ⬆️ to increase or ⬇️ to decrease chapter')
        return embed
    
    @discord.ui.button(label='⬆️', style=discord.ButtonStyle.green)
    async def increase_chapter(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Increase chapter by 1."""
        if interaction.user.id != int(self.user_id):
            await interaction.response.defer()
            return
        await self.update_chapter(self.current_chapter + 1, interaction)
    
    @discord.ui.button(label='⬇️', style=discord.ButtonStyle.red)
    async def decrease_chapter(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Decrease chapter by 1."""
        if interaction.user.id != int(self.user_id):
            await interaction.response.defer()
            return
        await self.update_chapter(max(0, self.current_chapter - 1), interaction)
    
    @discord.ui.button(label='✅ Done', style=discord.ButtonStyle.primary)
    async def done_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Close the interactive view."""
        if interaction.user.id != int(self.user_id):
            await interaction.response.defer()
            return
        
        user_data = load_user_books()
        book_entry = user_data[self.user_id][self.book_key]
        final_chapter = book_entry.get('chapter', 'Unknown')
        
        embed = discord.Embed(
            title='✅ Progress Saved',
            description=f'**{self.book_name}** → Chapter {final_chapter}',
            color=discord.Color.green()
        )
        await interaction.response.edit_message(embed=embed, view=None)
        self.stop()


@bot.tree.command(name='bookupdate', description='Update book progress with interactive buttons')
@app_commands.describe(book='Book name to update')
@app_commands.autocomplete(book=get_book_autocomplete_async)
async def book_update(interaction: discord.Interaction, book: str):
    """Interactive UI for updating book chapter progress."""
    try:
        user_id = str(interaction.user.id)
        user_data = load_user_books()
        
        if user_id not in user_data:
            await interaction.response.send_message(
                '❌ You have no books being tracked yet. Use `/bookprogress set` to add one!',
                ephemeral=True
            )
            return
        
        book_normalized = book.lower()
        
        # Try normalized key first, then try case-insensitive match for old data
        matched_key = None
        if book_normalized in user_data[user_id]:
            matched_key = book_normalized
        else:
            # Fallback: search case-insensitively through all books
            for stored_book in user_data[user_id].keys():
                if stored_book.lower() == book_normalized:
                    matched_key = stored_book
                    break
        
        if matched_key is None:
            await interaction.response.send_message(
                f'❌ You are not tracking "{book}". Use `/bookprogress view` to see your books!',
                ephemeral=True
            )
            return
        
        book_entry = user_data[user_id][matched_key]
        # Handle both old format (string) and new format (dict)
        if isinstance(book_entry, dict):
            chapter = int(book_entry.get('chapter', 0))
        else:
            chapter = int(book_entry)
        
        view = BookUpdateView(user_id, book, matched_key, chapter)
        embed = view.create_embed()
        await interaction.response.send_message(embed=embed, view=view)
    
    except ValueError:
        await interaction.response.send_message(
            '❌ Chapter number must be numeric',
            ephemeral=True
        )
    except Exception as e:
        await interaction.response.send_message(
            f'❌ Error: {str(e)[:100]}',
            ephemeral=True
        )
        print(f"Error in /bookupdate: {e}")


# Book Status Command (Guild-wide progress)
@bot.tree.command(name='bookstatus', description='View all users\' progress on a specific book')
@app_commands.describe(book='Book name to check')
@app_commands.autocomplete(book=get_book_autocomplete_async)
async def book_status(interaction: discord.Interaction, book: str):
    """Show guild-wide progress for a specific book."""
    try:
        user_data = load_user_books()
        
        # Collect all users tracking this book (case-insensitive)
        progress_list = []
        cover_image = None
        book_normalized = book.lower()
        for user_id, books in user_data.items():
            # Try normalized key first, then try case-insensitive match for old data
            if book_normalized in books:
                book_entry = books[book_normalized]
            else:
                # Fallback: search case-insensitively through all books
                matched_key = None
                for stored_book in books.keys():
                    if stored_book.lower() == book_normalized:
                        matched_key = stored_book
                        break
                if matched_key is None:
                    continue
                book_entry = books[matched_key]
            
            # Handle both old format (string) and new format (dict)
            if isinstance(book_entry, dict):
                chapter = book_entry.get('chapter', 'Unknown')
                if not cover_image:
                    cover_image = book_entry.get('image_url')
            else:
                chapter = book_entry
            
            try:
                user = await interaction.client.fetch_user(int(user_id))
                progress_list.append((user.name, chapter))
            except (discord.NotFound, discord.HTTPException):
                # User deleted or inaccessible; use ID
                progress_list.append((f"User {user_id}", chapter))
        
        if not progress_list:
            await interaction.response.send_message(
                f'📚 No one is tracking "{book}" yet.',
                ephemeral=True
            )
            return
        
        # Sort by chapter number (descending) if numeric, else alphabetically
        try:
            progress_list.sort(key=lambda x: -int(x[1]))
        except ValueError:
            progress_list.sort(key=lambda x: x[1])
        
        embed = discord.Embed(
            title=f'📖 "{book}" Progress',
            description=f'{len(progress_list)} reader{"s" if len(progress_list) != 1 else ""}',
            color=discord.Color.gold()
        )
        
        # Add cover image if available
        if cover_image:
            embed.set_thumbnail(url=cover_image)
        
        # Add progress entries
        for i, (username, chapter) in enumerate(progress_list[:25], 1):  # Limit to 25 entries
            embed.add_field(
                name=f'{i}. {username}',
                value=f'Chapter {chapter}',
                inline=False
            )
        
        if len(progress_list) > 25:
            embed.set_footer(text=f'... and {len(progress_list) - 25} more')
        
        await interaction.response.send_message(embed=embed)
    
    except Exception as e:
        await interaction.response.send_message(
            f'❌ Error: {str(e)[:100]}',
            ephemeral=True
        )
        print(f"Error in /bookstatus: {e}")


if not TOKEN:
    print('DISCORD_TOKEN not set. Please set it in the environment or in a .env file as DISCORD_TOKEN=...')
else:
    bot.run(TOKEN)
