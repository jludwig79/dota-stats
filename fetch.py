"""Dota Match Scraper

Todo:
    * MatchID projection to sample matches randomly vs. going in hero order
    * Test code with summoned units which can have items (e.g. Lone Druid,
      Arc Warden)
    * Check for duplicates before writing row key, maintain cached
      duplicate list... move batch_match from global to something
      else??
    * Make sure filtering is accurate
    * Go backwards a fix # of matches from most recent otherwise
      you get all turbo
"""
import time
import requests
import simplejson as json
import datetime as dt
import meta
import copy
import pandas as pd
import util
import time
import lzma
import sqlite3
import numpy as np
import logging
import os
import sys
import boto3
import aws
from dota_pb import dota_pb2

# Globals
MATCHES_PER_HERO=10
STEAM_KEY=os.environ['STEAM_KEY']

# Logging
log=logging.getLogger("dota")
log.setLevel(logging.DEBUG)
ch=logging.StreamHandler(sys.stdout)
fmt=logging.Formatter(fmt='%(asctime)s - %(name)s - %(levelname)s - %(message)s', 
                datefmt="%Y-%m-%dT%H:%M:%S %Z")
ch.setFormatter(fmt)
log.addHandler(ch)


PLAYER_FIELDS = [
    "account_id",
    "player_slot",
    "hero_id",
    "item_0",
    "item_1",
    "item_2",
    "item_3",
    "item_4",
    "item_5",
    "backpack_0",
    "backpack_1",
    "backpack_2",
    "kills",
    "deaths",
    "assists",  
    "leaver_status",
	"last_hits",
	"denies",
	"gold_per_min",
	"xp_per_min",
	"level",
	"hero_damage",
	"tower_damage",
	"hero_healing",
	"gold",
	"gold_spent",
	"scaled_hero_damage",
	"scaled_tower_damage",
	"scaled_hero_healing"
]

# Global dictionary for locally cached matches...
batch_match = {}
match_times = {}
this_aws=aws.AWS(os.environ["DOTA_MATCH_TABLE"])

def first_pass_match_times():
    """Loop through a seldom played hero to get dictionary of 
    matches vs. time to develop model for initial min and max 
    match_ids over a time window.
    
    Use Shadow Demon as a hero and high skill since it's a rare
    combination to cover the most time.
    """
    results_remaining=999    
    last_match=99999999999
    
    ms = []
    while(results_remaining>0):
        log.info("Fetching more matches....")
        url="https://api.steampowered.com/IDOTA2Match_570/GetMatchHistory/V001/"\
            "?key={0}&skill={1}&hero_id={2}&start_at_match_id={3}".\
            format(STEAM_KEY,3,79,last_match-1)
        rv=requests.get(url)    
        if rv.status_code==200:
            j=json.loads(rv.text)['result']
            results_remaining=j['results_remaining']
            for match in j['matches']:
                ms.append((match['match_id'],
                           match['start_time'],
                           dt.datetime.fromtimestamp(match['start_time']).isoformat()))
                last_match=match['match_id']
    return(ms)
    

def process_match(match_id,hero,skill,counter):
    SLEEP_SCHEDULE=[0.05,0.1,1.0,10,30,60,300,500,1000,2000,6000]
    fetched=False
    count=0
    while not(fetched) and count<10:
        time.sleep(SLEEP_SCHEDULE[count])
        try:
            rv=requests.get("https://api.steampowered.com/IDOTA2Match_570/GetMatchDetails/V001/?key={0}&match_id={1}".format(STEAM_KEY,match_id))
            if rv.status_code==200:
                fetched=True
        except:
            pass
        count=count+1

    # Still no match, bail...
    if rv.status_code!=200:
        log.warn("Bad HTTP-GET: {0}".format(rv.status_code))
        return(-44)

    txt="match ID {0} status code {1} hero {2:3} skill {3}".format(match_id, rv.status_code, hero, skill)
    
    match=json.loads(rv.text)['result']
    # Skill level as defined by API
    match['api_skill']=skill        
    # Partition on fetch time for subseqent batch
    # processing

    match['batch_time']=int(dt.datetime.fromtimestamp(match['start_time']).strftime("%Y%m%d%H"))
    
    key=str(match['batch_time'])+"_"+str(match['match_id'])
    if key in batch_match.keys():
        log.info("{0:20} {1}".format("Key exists",txt))
        return(-32)
    else:
        batch_match[key]=match['start_time']

    dt1=dt.datetime.utcfromtimestamp(match['start_time'])
            
    radiant_heroes=[]
    dire_heroes=[]

    # Bad game mode
    try:
        if not(meta.MODE_ENUM[match['game_mode']] in
            ["All Pick", "Captains Mode", "Random Draft", "Single Draft", "All Random", "Least Played"]):
            log.info("{0:20} {1}".format("Bad game mode",txt))
            return(-1)
    except:
        print("BAD MODE: {0} - {1}".format(match['game_mode'], match['match_id']))
        raise(exceptions.ValueError("Bad mode: {0}".format(match['game_mode'])))
                
    # Bail if zero length matches
    if (match['duration']<1200):
        log.info("{0:20} {1}".format("Short duration",txt))
        return(-2)
    
    # Bail for bot matches, etc...
    if (match['lobby_type'] in [-1,4,8]):
        log.info("{0:20} {1}".format("Bad lobby",txt))
        return(-3)
            
    match['calc_leaver']=0
    players=copy.deepcopy(match["players"])
    
    # Bail if Missing players
    if {} in players:
        log.info("{0:20} {1}".format("No players",txt))
        return(-4)
            
    new_players=[]
    for p in players:                
        # This might not be here due to bots
        try:
            if p['leaver_status']>match['calc_leaver']:
                match['calc_leaver']=p['leaver_status']                    
        except:
            pass
        
        player_slot=p['player_slot']

        if player_slot<=4:                
            radiant_heroes.append(p['hero_id'])                
        else:
            dire_heroes.append(p['hero_id'])

        k="hero-{0}".format(meta.HERO_DICT[p['hero_id']].lower().replace(" ","-"))
        match[k]=True

        pb2_player=dota_pb2.player()
        for t in PLAYER_FIELDS:
            setattr(pb2_player,t,p[t])
            p.pop(t)
        ability_pb_list=[]
        if 'ability_upgrades' in p:
            for ability in p.pop('ability_upgrades'):
                a=dota_pb2.ability()
                a.ability=ability['ability']
                a.time=ability['time']
                a.level=ability['level']
                ability_pb_list.append(a)
            pb2_player.ability_upgrades.extend(ability_pb_list)

        # Arc warden, Lone Druid...
        if "additional_units" in p:
            add_units=p.pop('additional_units')
            new_units = []
            for unit in add_units:
                au=dota_pb2.additional_unit()
                au.unitname=unit['unitname']
                for idx in range(6):
                    label="item_{0}".format(idx)
                    setattr(au,label,unit[label])
                for idx in range(3):
                    label="backpack_{0}".format(idx)
                    setattr(au,label,unit[label])
                new_units.append(au)
            pb2_player.additional_units.extend(new_units)                
        
        new_players.append(pb2_player)

    if match['calc_leaver']>1:
        log.info("{0:20} {1}".format("Leaver",txt))
        return(-43)

    # TODO: Add check for key in remote DB... otherwise counts 
    # get screwed up
        
    # Drop some other stuff we don't need
    match.pop("pre_game_duration")                                
    match.pop("positive_votes")
    match.pop("negative_votes")
    match.pop('match_seq_num')
    match.pop('tower_status_radiant')
    match.pop('tower_status_dire')
    match.pop('barracks_status_radiant')
    match.pop('barracks_status_dire')

    # Add to unit testing, radiant_name, dire_name might be 
    # present but missing
    if "radiant_name" in match.keys():
        match.pop('radiant_name')
    if "dire_name" in match.keys():
        match.pop('dire_name')
                    
    pb2_players = dota_pb2.players()
    pb2_players.players.extend(new_players)
    btxt=lzma.compress(pb2_players.SerializeToString())
    match['players']=boto3.dynamodb.types.Binary(btxt)
    try:            
        this_aws.dota_table.put_item(Item=match)

    except Exception as e:                                
        print(e)
        print(match)
        print("**** MATCH_ID: {0}".format(match['match_id']))
        import pdb
        pdb.set_trace()
        raise(e)

    log.info("{0:20} {1}".format("SUCCESS",txt))
    return(match['batch_time'])


def fetch_matches(hero, skill, conn):
    cursor=conn.cursor()
    start_at_match_id=9999999999
    counter=0
    start=time.time()
    while(counter<=MATCHES_PER_HERO):
        log.info("Fetching more matches....")
        url="https://api.steampowered.com/IDOTA2Match_570/GetMatchHistory/V001/?key={0}&skill={1}&start_at_match_id={2}&hero_id={3}".format(STEAM_KEY,skill,start_at_match_id,hero)
        rv=requests.get(url)
        log.info("Done fetching more matches....")
        if rv.status_code==200:
            j=json.loads(rv.text)['result']        
            if j['num_results']>0:        
                ms = []
                for match in j['matches']:
                    ms.append(match['match_id'])
                    batch_time=process_match(match['match_id'], hero, skill, counter)

                    # Log stats on successful return
                    if batch_time>0:
                        now_epoch=int((dt.datetime.utcnow()-dt.datetime(1970,1,1)).total_seconds())
                        cursor.execute('''select * from workflow_stats WHERE batch_time=?''',(batch_time,))
                        row = cursor.fetchone()
                        if row is None:
                            cursor.execute('''INSERT INTO workflow_stats(batch_time, updated_epoch, fetch, pair) VALUES (?,?,?,?)''',(batch_time,now_epoch,1,0))
                            conn.commit()
                        else:
                            sql='''UPDATE workflow_stats SET updated_epoch=?, fetch=? WHERE batch_time=?'''
                            cursor.execute(sql, (now_epoch, row[2]+1, batch_time))
                            conn.commit()
 
                    counter=counter+1
            start_at_match_id=min(ms)-1
        else:
            print("Bad return code")
    print("Matches per minute: {0}".format(60*counter/(time.time()-start)))

if __name__=="__main__":
    conn=sqlite3.connect(os.environ['WORKFLOW_DB'])
    c=conn.cursor()

    c.execute("select * from sqlite_master where type = 'table' and name='workflow_stats'")
    row=c.fetchone()
    if row is not None:
        log.info("Found existing workflow stats table")
    else:
        log.info("Creating new table workflow_stats")
        c.execute("CREATE TABLE IF NOT EXISTS workflow_stats (batch_time INTEGER, updated_epoch INTEGER, fetch INTEGER, pair INTEGER)")
            
    heroes_random=list(meta.HERO_DICT.keys())
    idx=np.random.choice(range(len(heroes_random)),len(heroes_random),replace=False)
    heroes_random=[heroes_random[t] for t in idx]

    while True:
        for h in heroes_random:
            for s in [1,2,3]:
                log.info("Hero: {0}\t\tSkill: {1}".format(meta.HERO_DICT[h],s))
                fetch_matches(h,s,conn)
