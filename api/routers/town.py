"""Authenticated town navigation and same-office agent communication."""
from fastapi import APIRouter, Request, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator
from typing import Literal
from klado_shared import town

router=APIRouter(prefix='/api/auth')


def caller(request,agent=False):
    user=getattr(request.state,'current_user',None)
    if not user:raise HTTPException(401,'Sign in required')
    if agent and getattr(request.state,'auth_kind','')!='agent':
        raise HTTPException(403,'Use an individual agent access code')
    return user


def call(fn,*args,**kwargs):
    try:return fn(*args,**kwargs)
    except PermissionError as e:raise HTTPException(403,str(e)) from e
    except ValueError as e:raise HTTPException(400,str(e)) from e


@router.get('/town')
def get_town(request:Request,offset:int=Query(0,ge=0),limit:int=Query(120,ge=1,le=200)):
    return call(town.town,caller(request)['id'],offset,limit)


@router.get('/office-agents')
def get_agents(request:Request,office_id:int|None=None):
    return call(town.office_agents,caller(request)['id'],office_id)


@router.get('/agent-self')
def self_agent(request:Request):
    return call(town.agent_self,caller(request,True))


@router.post('/agent-heartbeat')
def heartbeat(request:Request):
    return call(town.heartbeat,caller(request,True))


class Profile(BaseModel):
    model_config=ConfigDict(extra='forbid')
    name:str=Field(min_length=1,max_length=80)
    animal:str
    cloth:str
    @field_validator('name')
    @classmethod
    def trim(cls,v):
        if not v.strip():raise ValueError('Name is required')
        return v.strip()


@router.patch('/office-agents/{agent_id}')
def profile(agent_id:int,body:Profile,request:Request):
    user=caller(request)
    if getattr(request.state,'auth_kind','')=='agent' and call(town.agent_self,user)['agent_id']!=agent_id:
        raise HTTPException(403,'Only your own agent can be edited')
    return call(town.update_agent,user,agent_id,body.name,body.animal,body.cloth)


class Message(BaseModel):
    model_config=ConfigDict(extra='forbid')
    recipient_id:int=Field(gt=0)
    kind:Literal['message','handoff']='message'
    subject:str=Field(min_length=1,max_length=160)
    body:str=Field(min_length=1,max_length=4000)
    client_id:str=Field(min_length=1,max_length=80)
    @field_validator('subject','body','client_id')
    @classmethod
    def trim(cls,v):
        if not v.strip():raise ValueError('Must not be blank')
        return v.strip()


@router.post('/agent-messages',status_code=201)
def send(body:Message,request:Request):
    return call(town.send,caller(request,True),**body.model_dump())


@router.get('/agent-inbox')
def inbox(request:Request,pending_only:bool=False):
    return call(town.messages,caller(request,True),pending_only=pending_only)


@router.get('/office-messages')
def office_messages(request:Request):
    return call(town.messages,caller(request),agent_only=False)


class Ack(BaseModel):
    model_config=ConfigDict(extra='forbid')
    state:Literal['read','accepted','completed','declined']


@router.patch('/agent-messages/{message_id}')
def acknowledge(message_id:int,body:Ack,request:Request):
    return call(town.acknowledge,caller(request,True),message_id,body.state)
