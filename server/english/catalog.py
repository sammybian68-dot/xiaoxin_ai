"""英语场景对话的 20 课大纲：贴着译林版（三起，2024）三年级上册的单元主题排，前面简单后面难。
每课：两个角色（孩子可以选一个演）、场景画面描述、要用的句型和词。剧本由 build.py 按这个大纲让大模型写。

角色的长相写死在 LOOK 里，所以同一个角色（Lily、Mr. Bear……）在不同课里长得一样。"""

from __future__ import annotations

# 角色外形（画透明底单人像用），名字 → (英文描述, 中文名, 声音)。声音只用英音（见记忆 xiaoxin-english-speaking）
LOOK = {
    "lily": ("a cute 8 year old Chinese girl with two short pigtails, yellow dress, red shoes", "莉莉", "en_female_emily_mars_bigtts"),
    "tom": ("a cheerful 8 year old boy with short brown hair, blue striped T-shirt, green shorts, sneakers", "汤姆", "en_male_dryw_mars_bigtts"),
    "bear": ("a friendly brown bear shopkeeper wearing a white apron and a small striped paper hat", "熊先生", "en_male_smith_mars_bigtts"),
    "panda": ("a friendly giant panda wearing a red scarf", "熊猫", "en_male_dryw_mars_bigtts"),
    "rabbit": ("a small white rabbit girl with long ears, wearing a pink dress and a bow", "兔子", "en_female_anna_mars_bigtts"),
    "teacher": ("a kind young female English teacher with a ponytail and glasses, cardigan and long skirt", "老师", "en_female_sarah_mars_bigtts"),
    "mum": ("a smiling Chinese mother in her thirties with shoulder-length hair, cardigan and jeans", "妈妈", "en_female_sarah_mars_bigtts"),
    "grandpa": ("a gentle Chinese grandfather with white hair and glasses, wearing a grey vest", "爷爷", "en_male_smith_mars_bigtts"),
    "doctor": ("a friendly doctor cat wearing a white coat and a stethoscope", "猫医生", "en_female_stokie_uranus_bigtts"),
    "monkey": ("a playful little monkey wearing a green cap", "小猴", "en_male_dryw_mars_bigtts"),
    "sam": ("a friendly 9 year old girl with short curly hair, red hoodie, jeans", "萨姆", "en_female_anna_mars_bigtts"),
}
VOICE_RES = {"en_female_stokie_uranus_bigtts": "seed-tts-2.0"}   # 其他都在 seed-tts-1.0

# 课程：id、英文名、中文名、对应单元、两个角色（第一个是孩子默认演的）、场景、要用的句型 / 词
LESSONS = [
    ("hello", "Hello!", "你好", "U1", ["lily", "tom"], "a sunny school gate in the morning with a small flower bed and a bench",
     "greetings: Hello / Hi / Good morning / Goodbye / See you; I'm ..."),
    ("name", "What's Your Name?", "你叫什么名字", "U2", ["lily", "panda"], "a playground with a slide and a sandpit",
     "What's your name? My name is ... / Nice to meet you. / How old are you? I'm eight."),
    ("are_you", "Are You Su Hai?", "你是苏海吗", "U3", ["tom", "lily"], "a school hallway with lockers and children's drawings on the wall",
     "Are you ...? Yes, I am. / No, I'm not. / Is she ...? Yes, she is."),
    ("friend", "This Is My Friend", "这是我的朋友", "U4", ["lily", "sam"], "a sunny park with trees and a little pond",
     "This is my friend ... / Hello, ... / Nice to meet you, too. / Let's play."),
    ("mother", "She's My Mother", "她是我的妈妈", "U5", ["lily", "teacher"], "the school gate in the afternoon, parents waiting",
     "Who's she? She's my mother. / Who's he? He's my father. / Good afternoon."),
    ("grandpa", "Is He Your Grandpa?", "他是你爷爷吗", "U6", ["tom", "lily"], "a cozy living room with family photos on a shelf",
     "Is he your grandpa? Yes, he is. / Is she your grandma? No, she isn't. She's my aunt."),
    ("birthday", "Happy Birthday!", "生日快乐", "U7", ["lily", "tom"], "a living room decorated for a birthday party, balloons, a cake with candles",
     "Happy birthday! / This is for you. / Thank you. / How old are you? I'm nine. / Let's eat the cake."),
    ("help", "I Can Help You", "我来帮你", "U8", ["lily", "mum"], "a bright home kitchen with a table and plants",
     "Can I help you? / I can wash the cups. / Here you are. / Thank you. / You're welcome."),
    ("icecream", "Ice Cream Shop", "冰淇淋店", "生活", ["lily", "bear"], "interior of a cozy ice cream shop, a long wooden counter, display case with pink, white and brown ice cream",
     "I'd like ... / What colour? Pink, white or brown? / One scoop or two? / How much is it? / Here you are."),
    ("fruit", "Fruit Market", "水果摊", "生活", ["lily", "bear"], "a small fruit market stall with apples, bananas, oranges and tomatoes under a striped awning",
     "What would you like? / I'd like some apples, please. / How many? Three, please. / Here you are."),
    ("colours", "Colours in the Park", "公园里的颜色", "生活", ["tom", "lily"], "a park with colourful flowers, a red kite in the sky, a blue bench",
     "What colour is it? It's red. / Look at the kite. / It's nice. / I like yellow."),
    ("zoo", "At the Zoo", "动物园", "生活", ["lily", "monkey"], "a sunny zoo path with a giraffe and an elephant behind fences",
     "Look at the ...! / It's big. It's small. / What's this? It's a panda. / I like monkeys."),
    ("classroom", "In the Classroom", "教室里", "生活", ["tom", "teacher"], "a bright primary school classroom with small desks and a blackboard",
     "Stand up, please. / Sit down, please. / Open your book. / May I come in? Yes, please."),
    ("pencil", "Is This Your Pencil?", "这是你的铅笔吗", "生活", ["lily", "tom"], "two school desks with pencils, rulers, rubbers and pencil cases on them",
     "Is this your pencil? Yes, it is. / No, it isn't. / Where's my ruler? Here it is."),
    ("breakfast", "Breakfast Time", "早餐时间", "生活", ["tom", "mum"], "a family breakfast table with bread, milk, eggs and juice, morning sunlight",
     "Good morning, Mum. / Would you like some milk? Yes, please. / No, thank you. / I'm hungry."),
    ("doctor", "I Don't Feel Well", "去看医生", "生活", ["lily", "doctor"], "a friendly cartoon clinic room with a little bed and a height chart",
     "What's wrong? / I have a cold. / I'm tired. / Drink some water. / Have a rest."),
    ("rabbit", "My New Pet", "我的新宠物", "生活", ["lily", "rabbit"], "a garden with carrots growing and a small wooden pet house",
     "Is it a cat? No, it isn't. It's a rabbit. / It's white. / Its ears are long. / I like it."),
    ("weather", "What's the Weather Like?", "天气怎么样", "生活", ["tom", "lily"], "a window looking out on rain, umbrellas and raincoats by the door",
     "It's rainy today. / Put on your coat. / Take your umbrella. / It's sunny now. Let's go out."),
    ("toys", "In the Toy Shop", "玩具店", "生活", ["tom", "bear"], "a toy shop with shelves of teddy bears, balls, cars and dolls",
     "Can I help you? / I'd like a ball. / What about this car? / It's nice. How much? / Ten yuan."),
    ("picnic", "A Picnic", "野餐", "生活", ["lily", "grandpa"], "a picnic blanket on green grass with a basket, sandwiches and fruit, a lake behind",
     "Let's have a picnic. / Would you like a sandwich? / Yes, please. / It's yummy. / Have some juice."),
]
