# Uttale
A fast and efficient search and playback tool for audio files.

## Table of Contents
* [Overview](#overview)
* [Features](#features)
* [Getting Started](#getting-started)
* [Server](#server)
* [Client](#client)
* [Usage](#usage)

## Overview
The Uttale Project is designed to provide a simple and efficient way to search
and playback audio files. It consists of two main components: a server and a
client.

## Features
* Fast and efficient search functionality
* Playback of audio segments
* Support for multiple audio formats
* Reindexing of subtitle files for improved search performance

## Getting Started
To get started with the Uttale Project, follow these steps:

1. Clone the repository: `git clone https://github.com/your-username/uttale.git`
2. Install the required dependencies: `pip install -r requirements.txt`
3. Start the server: `python server.py`
4. Start the client: `python quick_ui.py`

## Server
The server is responsible for handling search queries and playback requests. It
uses a database to store the audio files and their corresponding metadata.

### Server Endpoints
* `/uttale/Scopes`: Returns a list of scopes for a given search query
* `/uttale/Search`: Returns a list of search results for a given query and scope
* `/uttale/Audio`: Returns an audio segment for a given filename, start time, and end time
* `/uttale/Reindex`: Triggers a reindexing of the subtitle files

## Client
The client is a graphical user interface (GUI) that allows users to search for
audio files and playback audio segments.

### Client Features
* Search bar for searching audio files
* List of search results with playback buttons
* Playback of audio segments

## Usage
To use the Uttale Project, follow these steps:

1. Start the server and client
2. Enter a search query in the search bar
3. Select a scope from the list of scopes
4. Click the play button next to a search result to playback the audio segment
