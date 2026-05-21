class Post < ApplicationRecord
  belongs_to :user
  has_many :comments, dependent: :destroy
  has_many :taggables, as: :taggable
  has_many :tags, through: :taggables

  validates :title, presence: true
  validates :body, presence: true
end
