import datetime
from datetime import datetime,timedelta

class memb:
    def __init__(self,_name,_id):
        self.name=_name
        self.userid=_id
        self.time=timedelta(0)
        self._ing=False
        self.timestamp=datetime.now()
        
    def update_in(self): #입장
        self._ing=True
        self.timestamp=datetime.now()

    def update_out(self): #퇴장
        self._ing=False
        self.time += datetime.now() -self.timestamp

    def printing(self): # "접속 시간 : 닉네임(id)" 리턴
        rttime = (str(self.time).split('.')[0])
        return f"{rttime} : {self.name}({self.userid})\n"     

    def ing(self):#접속중 정보
        return self._ing

class membermanager:
    def __init__(self):
        self.members={}
        self.timestamp=datetime.now()

    def update(self):
        for _id in self.members:
            if self.members[_id].ing():
                self.members[_id].update_out()
                self.members[_id].update_in() 

    def reset(self):
        self.timestamp=datetime.now()

        for _id in self.members:
            if not self.members[_id].ing():
                del self.members[_id]

    def add(self,mem): 
        self.members[mem.userid]=mem

    def making(self,_name,_id,inout): #입장 및 퇴장 시
        if _id in self.members:
            if self.members[_id].name != _name:#닉네임 변경 체크
                self.members[_id].name = _name

            if(inout=='in'):
                self.members[_id].update_in()
            if(inout=='out'):
                self.members[_id].update_out()

        elif(inout=='in'):
                self.add(memb(_name, _id))
                self.members[_id].update_in()
          

    def printing(self):
        self.sorting()
        rt=(str(self.timestamp).split('.')[0])
        rt+='부터 시작된 기록입니다.\n'
        rt+=''.join([mem.printing() for mem in self.members.values()])
        return rt

    def sorting(self): # 접속 시간 내림차순 정렬
        self.members= {k: v for k, v in sorted(self.members.items(), key=lambda item: item[1].time, reverse=True)}
    
